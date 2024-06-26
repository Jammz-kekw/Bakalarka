"""
    Prevzatý kód
"""

import numpy as np
import wandb

from editable_stain_xaicyclegan2.setup.settings_module import Settings
from editable_stain_xaicyclegan2.setup.logging_utils import RunningMeanStack, normalize_image


class WandbModule:
    """
    This class creates a wandb run and saves the config.
    """

    def __init__(self, settings: Settings):
        self.run = wandb.init(
            project=settings.project,
            group=settings.group,
            name=settings.name,
            notes=settings.notes,
            resume=settings.resume,
            mode=settings.mode,
            dir=settings.log_dir,
            config=settings.cfg_dict
        )

        self.run.log_code(".")
        self.model_log = wandb.Artifact('xai-cyclegan_model', type='model', description=settings.model_notes)

        self.step = 0
        self.log_frequency = settings.log_frequency
        self.model_file = None

        self.generator_he_to_p63_running_loss_avg = RunningMeanStack(self.log_frequency)
        self.generator_p63_to_he_running_loss_avg = RunningMeanStack(self.log_frequency)
        self.discriminator_he_running_loss_avg = RunningMeanStack(self.log_frequency)
        self.discriminator_p63_running_loss_avg = RunningMeanStack(self.log_frequency)
        self.cycle_he_running_loss_avg = RunningMeanStack(self.log_frequency)
        self.cycle_p63_running_loss_avg = RunningMeanStack(self.log_frequency)
        self.total_running_loss_avg = RunningMeanStack(self.log_frequency)
        self.context_running_loss_avg = RunningMeanStack(self.log_frequency)
        self.cycle_context_running_loss_avg = RunningMeanStack(self.log_frequency)

    def log(self, epoch):
        self.run.log({
            "he_to_p63_generator_loss": self.generator_he_to_p63_running_loss_avg.mean,
            "p63_to_he_generator_loss": self.generator_p63_to_he_running_loss_avg.mean,
            "he_discriminator_loss": self.discriminator_he_running_loss_avg.mean,
            "p63_discriminator_loss": self.discriminator_p63_running_loss_avg.mean,
            "he_cycle_loss": self.cycle_he_running_loss_avg.mean,
            "p63_cycle_loss": self.cycle_p63_running_loss_avg.mean,
            "total_generator_loss": self.total_running_loss_avg.mean,
            "context_loss": self.context_running_loss_avg.mean,
            "cycle_context_loss": self.cycle_context_running_loss_avg.mean,
            "epoch": epoch,
        }, step=self.step)

    def log_image(self, real_image_pair, gen_image_pair, recon_image_pair):
        # Concatenate the images horizontally to create rows
        row_0 = np.concatenate((
            normalize_image(real_image_pair[0]),
            normalize_image(gen_image_pair[1]),
            normalize_image(recon_image_pair[0]),
        ), axis=1)

        row_1 = np.concatenate((
            normalize_image(real_image_pair[1]),
            normalize_image(gen_image_pair[0]),
            normalize_image(recon_image_pair[1]),
        ), axis=1)

        # Concatenate the rows vertically to create the final image
        merged_image = np.concatenate((row_0, row_1), axis=0)

        self.run.log({
            "generation_results": wandb.Image(merged_image, caption="Top row HE->P63, Bottom P63->HE, L to R orig., transf., reconstr."),
        }, step=self.step)

    def log_image_paired(self, real_image_pair, gen_image_pair, recon_image_pair):
        # Concatenate the images horizontally to create rows
        row_0 = np.concatenate((
            normalize_image(real_image_pair[0]),
            normalize_image(gen_image_pair[1]),
            normalize_image(recon_image_pair[0]),
        ), axis=1)

        row_1 = np.concatenate((
            normalize_image(real_image_pair[1]),
            normalize_image(gen_image_pair[0]),
            normalize_image(recon_image_pair[1]),
        ), axis=1)

        # Concatenate the rows vertically to create the final image
        merged_image = np.concatenate((row_0, row_1), axis=0)

        self.run.log({
            "generation_results": wandb.Image(merged_image, caption="Paired"),
        }, step=self.step)

    def log_model(self, model_file):
        if self.model_file is None:
            self.model_file = model_file
            self.model_log.add_file(model_file)

        self.run.log_artifact(self.model_log)
