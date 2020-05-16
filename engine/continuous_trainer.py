import logging

import torch
from ignite.engine import Engine, Events
from ignite.handlers import Timer
from ignite.metrics import RunningAverage

from engine.inference import get_valid_eval_map, eval_multi_dataset
from engine.trainer import Run
from loss import Loss
from tools.component import TrainComponent
from utils.tensorboardX_log import TensorBoardXLog

logger = logging.getLogger("reid_baseline.continue")


def create_supervised_trainer(source_model,
                              current_model,
                              optimizer,
                              groupLoss: Loss,
                              apex=False,
                              device=None):
    def _update(engine, batch):
        source_model.eval()
        current_model.train()
        optimizer.zero_grad()
        groupLoss.optimizer_zero_grad()

        # data
        img, target = batch
        img = img.to(device)
        target = target.to(device)

        # source_feat
        _, source_feat_c = source_model(img)

        # current_feat
        current_feat_t, current_feat_c, current_cls_score = current_model(img)

        loss_values = {}
        loss_args = {"feat_t": current_feat_t,
                     "feat_c": current_feat_c,
                     "cls_score": current_cls_score,
                     "target": target,
                     "target_feat_c": source_feat_c}

        # train current model
        loss = torch.tensor(0.0, requires_grad=True).to(device)
        for name, loss_fn in groupLoss.loss_function_map.items():
            loss_temp = loss_fn(**loss_args)
            loss += loss_temp
            loss_values[name] = loss_temp.item()

        if apex:
            from apex import amp
            with amp.scale_loss(loss, optimizer) as scaled_loss:
                scaled_loss.backward()
        else:
            loss.backward()

        optimizer.step()
        groupLoss.optimizer_step()

        # compute acc
        acc = (current_cls_score.max(1)[1] == target).float().mean()
        loss_values["Loss"] = loss.item()
        loss_values["Acc"] = acc.item()
        return loss_values

    return Engine(_update)


def do_continuous_train(cfg,
                        train_loader,
                        valid,
                        source_tr_comp: TrainComponent,
                        current_tr_comp: TrainComponent,
                        saver):
    tb_log = TensorBoardXLog(cfg, saver.save_dir)

    device = cfg.MODEL.DEVICE

    trainer = create_supervised_trainer(source_tr_comp.model,
                                        current_tr_comp.model,
                                        current_tr_comp.optimizer,
                                        current_tr_comp.loss,
                                        device=device,
                                        apex=cfg.APEX.IF_ON)

    saver.to_save = {'trainer': trainer,
                     'model': current_tr_comp.model}
    # 'optimizer': tr_comp.optimizer,
    # 'center_param': tr_comp.loss_center,
    # 'optimizer_center': tr_comp.optimizer_center}

    trainer.add_event_handler(Events.EPOCH_COMPLETED(every=cfg.SAVER.CHECKPOINT_PERIOD),
                              saver.train_checkpointer,
                              saver.to_save)

    # multi-valid-dataset
    validation_evaluator_map = get_valid_eval_map(cfg, device, current_tr_comp.model, valid)

    timer = Timer(average=True)
    timer.attach(trainer,
                 start=Events.EPOCH_STARTED,
                 resume=Events.ITERATION_STARTED,
                 pause=Events.ITERATION_COMPLETED,
                 step=Events.ITERATION_COMPLETED)

    # average metric to attach on trainer
    names = ["Acc", "Loss"]
    names.extend(current_tr_comp.loss.loss_function_map.keys())

    for n in names:
        RunningAverage(output_transform=Run(n)).attach(trainer, n)

    # TODO start epoch
    @trainer.on(Events.STARTED)
    def start_training(engine):
        engine.state.epoch = 0

    @trainer.on(Events.EPOCH_STARTED)
    def adjust_learning_rate(engine):
        current_tr_comp.scheduler.step()
        current_tr_comp.loss.scheduler_step()

    @trainer.on(Events.ITERATION_COMPLETED(every=cfg.TRAIN.LOG_ITER_PERIOD))
    def log_training_loss(engine):
        message = f"Epoch[{engine.state.epoch}], " + \
                  f"Iteration[{engine.state.iteration}/{len(train_loader)}], " + \
                  f"Lr: {current_tr_comp.scheduler.get_lr()[0]:.2e}, " + \
                  f"Loss: {engine.state.metrics['Loss']:.4f}, " + \
                  f"Acc: {engine.state.metrics['Acc']:.4f}, "

        for loss_name in current_tr_comp.loss.loss_function_map.keys():
            message += f"{loss_name}: {engine.state.metrics[loss_name]:.4f}, "

        if current_tr_comp.loss.xent and current_tr_comp.loss.xent.learning_weight:
            message += f"xentWeight: {current_tr_comp.loss.xent.uncertainty.item():.4f}, "

        if current_tr_comp.loss.triplet and current_tr_comp.loss.triplet.learning_weight:
            message += f"tripletWeight: {current_tr_comp.loss.triplet.uncertainty.item():.4f}, "

        if current_tr_comp.loss.center and current_tr_comp.loss.center.learning_weight:
            message += f"centerWeight: {current_tr_comp.loss.center.uncertainty.item():.4f}, "

        logger.info(message)

    # adding handlers using `trainer.on` decorator API
    @trainer.on(Events.EPOCH_COMPLETED)
    def print_times(engine):
        logger.info('Epoch {} done. Time per batch: {:.3f}[s] Speed: {:.1f}[samples/s]'
                    .format(engine.state.epoch, timer.value() * timer.step_count,
                            train_loader.batch_size / timer.value()))
        logger.info('-' * 80)
        timer.reset()

    @trainer.on(Events.EPOCH_COMPLETED(every=cfg.EVAL.EPOCH_PERIOD),
                saver=saver)
    def log_validation_results(engine, saver):

        logger.info(f"Valid - Epoch: {engine.state.epoch}")

        sum_result = eval_multi_dataset(device, validation_evaluator_map, valid)

        if saver.best_result < sum_result:
            logger.info(f'Save best: {sum_result:.4f}')
            saver.save_best_value(sum_result)
            saver.best_checkpointer(engine, saver.to_save)
            saver.best_result = sum_result
        else:
            logger.info(f"Not best: {saver.best_result:.4f} > {sum_result:.4f}")
        logger.info('-' * 80)

    tb_log.attach_handler(trainer, current_tr_comp.model, current_tr_comp.optimizer)

    # self.tb_logger.attach(
    #     validation_evaluator,
    #     log_handler=ReIDOutputHandler(tag="valid", metric_names=["r1_mAP"], another_engine=trainer),
    #     event_name=Events.EPOCH_COMPLETED,
    # )

    trainer.run(train_loader, max_epochs=cfg.TRAIN.MAX_EPOCHS)

    tb_log.close()
