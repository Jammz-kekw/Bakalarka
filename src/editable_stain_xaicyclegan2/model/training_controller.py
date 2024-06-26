"""
    Prevzatý kód
"""

import itertools
from typing import Callable, Union

import torch
from torch.autograd import Variable
from torch.utils.data import DataLoader

# from torchmetrics.functional.image.ssim import structural_similarity_index_measure as ssim

from editable_stain_xaicyclegan2.model.dataset import DatasetFromFolder
from editable_stain_xaicyclegan2.model.explanation import ExplanationController
from editable_stain_xaicyclegan2.model.mask import get_mask
from editable_stain_xaicyclegan2.model.model import Generator, Discriminator
from editable_stain_xaicyclegan2.model.utils import LambdaLR, ImagePool

from editable_stain_xaicyclegan2.setup.settings_module import Settings
from editable_stain_xaicyclegan2.setup.wandb_module import WandbModule

L_RANGE = 1.68976005407

TensorType = Union[Variable, torch.Tensor]


class TrainingController:

    def __init__(self, settings: Settings | None, wandb_module: WandbModule | None, saved_model_obj: dict = None):
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        self.half_precision = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        self.settings = settings
        self.wandb_module = wandb_module

        self.latest_generator_loss = None
        self.latest_discriminator_he_loss = None
        self.latest_discriminator_p63_loss = None
        self.latest_identity_loss = None
        self.latest_cycle_loss = None
        self.latest_context_loss = None
        self.latest_cycle_context_loss = None

        if saved_model_obj:
            settings = saved_model_obj['settings']

        # region Initialize data loaders
        self.train_he_data = DatasetFromFolder(settings.data_root, settings.data_train_he, settings.norm_dict)
        self.train_he = DataLoader(dataset=self.train_he_data, batch_size=settings.batch_size,
                                   shuffle=True, pin_memory=True, num_workers=4)

        # train data can be shuffled in order to get better results

        self.train_p63_data = DatasetFromFolder(settings.data_root, settings.data_train_p63, settings.norm_dict)
        self.train_p63 = DataLoader(dataset=self.train_p63_data, batch_size=settings.batch_size,
                                    shuffle=True, pin_memory=True, num_workers=4)

        self.test_he_data = DatasetFromFolder(settings.data_root, settings.data_test_he, settings.norm_dict)
        self.test_he = DataLoader(dataset=self.test_he_data, batch_size=settings.batch_size,
                                  shuffle=False, pin_memory=True, num_workers=4)

        self.test_p63_data = DatasetFromFolder(settings.data_root, settings.data_test_p63, settings.norm_dict)
        self.test_p63 = DataLoader(dataset=self.test_p63_data, batch_size=settings.batch_size,
                                   shuffle=False, pin_memory=True, num_workers=4)

        self.paired_he_data = DatasetFromFolder(settings.data_root, "paired_he", None)
        self.paired_he = DataLoader(dataset=self.paired_he_data, batch_size=settings.batch_size,
                                    shuffle=False, pin_memory=True, num_workers=4)

        self.paired_ihc_data = DatasetFromFolder(settings.data_root, "paired_ihc", None)
        self.paired_ihc = DataLoader(dataset=self.paired_ihc_data, batch_size=settings.batch_size,
                                     shuffle=False, pin_memory=True, num_workers=4)
        # endregion

        # region Initialize models
        generator_params = (settings.generator_downconv_filters, settings.num_resnet_blocks, settings.channels, settings.channels)
        discriminator_params = (settings.discriminator_downconv_filters, settings.channels)

        self.generator_he_to_p63 = Generator(*generator_params)
        self.generator_p63_to_he = Generator(*generator_params)
        self.discriminator_he = Discriminator(*discriminator_params)
        self.discriminator_p63 = Discriminator(*discriminator_params)
        self.discriminator_he_mask = Discriminator(*discriminator_params)
        self.discriminator_p63_mask = Discriminator(*discriminator_params)

        if saved_model_obj:
            self.generator_he_to_p63.load_state_dict(saved_model_obj['generator_he_to_p63_state_dict'])
            self.generator_p63_to_he.load_state_dict(saved_model_obj['generator_p63_to_he_state_dict'])
            self.discriminator_he.load_state_dict(saved_model_obj['discriminator_he_state_dict'])
            self.discriminator_p63.load_state_dict(saved_model_obj['discriminator_p63_state_dict'])
            self.discriminator_he_mask.load_state_dict(saved_model_obj['discriminator_he_mask_state_dict'])
            self.discriminator_p63_mask.load_state_dict(saved_model_obj['discriminator_p63_mask_state_dict'])

            self.generator_he_to_p63.eval()
            self.generator_p63_to_he.eval()
            self.discriminator_he.eval()
            self.discriminator_p63.eval()
            self.discriminator_he_mask.eval()
            self.discriminator_p63_mask.eval()

        self.generator_he_to_p63.to(self.device).to(memory_format=torch.channels_last)  # noqa
        self.generator_p63_to_he.to(self.device).to(memory_format=torch.channels_last)  # noqa
        self.discriminator_he.to(self.device).to(memory_format=torch.channels_last)  # noqa
        self.discriminator_p63.to(self.device).to(memory_format=torch.channels_last)  # noqa
        self.discriminator_he_mask.to(self.device).to(memory_format=torch.channels_last)  # noqa
        self.discriminator_p63_mask.to(self.device).to(memory_format=torch.channels_last)  # noqa
        # endregion

        # region Initialize wandb model watching
        if saved_model_obj is None:
            if torch.multiprocessing.current_process().name == 'MainProcess':
                self.wandb_module.run.watch(
                    (
                        self.generator_he_to_p63,
                        self.generator_p63_to_he,
                        self.discriminator_he,
                        self.discriminator_he_mask,
                        self.discriminator_p63,
                        self.discriminator_p63_mask
                    ),
                    log="all",
                    log_freq=wandb_module.log_frequency,
                    log_graph=True
                )
        # endregion

        # region Initialize explanation classes
        self.he_explainer = ExplanationController(
            self.discriminator_he.loss_fake,
            self.discriminator_he_mask.loss_fake,
            settings.lambda_mask_adversarial_ratio,
            settings.explanation_ramp_type
        )

        self.p63_explainer = ExplanationController(
            self.discriminator_p63.loss_fake,
            self.discriminator_p63_mask.loss_fake,
            settings.lambda_mask_adversarial_ratio,
            settings.explanation_ramp_type
        )

        # P63 explainer contains P63 discriminator, used to explain P63 generation mistakes to the H&E gen.
        # Therefore, we have to assign P63 explainer to the HE to P63 generator.
        self.generator_he_to_p63.final.register_backward_hook(self.p63_explainer.explanation_hook)
        self.generator_p63_to_he.final.register_backward_hook(self.he_explainer.explanation_hook)
        # endregion

        # region Initialize loss functions
        self.criterion_GAN = torch.nn.MSELoss()
        self.criterion_pixel_wise = torch.nn.L1Loss()
        # endregion

        # region Initialize optimizers
        if saved_model_obj is None:
            discriminator_he_params = itertools.chain(
                self.discriminator_he.parameters(),
                self.discriminator_he_mask.parameters()
            )

            discriminator_p63_params = itertools.chain(
                self.discriminator_p63.parameters(),
                self.discriminator_p63_mask.parameters()
            )

            self.generator_optimizer = torch.optim.NAdam(
                itertools.chain(self.generator_he_to_p63.parameters(), self.generator_p63_to_he.parameters()),
                lr=settings.lr_generator, betas=(settings.beta1, settings.beta2), weight_decay=0.001, decoupled_weight_decay=True
            )

            self.discriminator_he_optimizer = torch.optim.NAdam(
                discriminator_he_params,
                lr=settings.lr_discriminator, betas=(settings.beta1, settings.beta2), weight_decay=0.001, decoupled_weight_decay=True
            )

            self.discriminator_p63_optimizer = torch.optim.NAdam(
                discriminator_p63_params,
                lr=settings.lr_discriminator, betas=(settings.beta1, settings.beta2), weight_decay=0.001, decoupled_weight_decay=True
            )

            self.lr_generator_scheduler = torch.optim.lr_scheduler.LambdaLR(
                self.generator_optimizer, lr_lambda=LambdaLR(settings.epochs, settings.decay_epoch).step
            )

            self.lr_discriminator_he_scheduler = torch.optim.lr_scheduler.LambdaLR(
                self.discriminator_he_optimizer, lr_lambda=LambdaLR(settings.epochs, settings.decay_epoch).step
            )

            self.lr_discriminator_p63_scheduler = torch.optim.lr_scheduler.LambdaLR(
                self.discriminator_p63_optimizer, lr_lambda=LambdaLR(settings.epochs, settings.decay_epoch).step
            )
        # endregion

        # region Initialize image pool
        if saved_model_obj is None:
            pool_size = settings.pool_size
            self.fake_he_pool = ImagePool(pool_size)
            self.fake_p63_pool = ImagePool(pool_size)
        # endregion

    # general function to get loss based on chosen criterion
    def get_loss(self, tensor: TensorType, loss_function: Callable, target_function: Callable) -> torch.Tensor:
        return loss_function(tensor, Variable(target_function(tensor.size()).to(self.device).to(memory_format=torch.channels_last)))

    # get total loss for mask discriminator
    def get_total_mask_disc_loss(self, real: TensorType, mask: TensorType,
                                 fake: TensorType, discriminator_mask: Discriminator) -> torch.Tensor:
        discriminator_mask_real_decision = discriminator_mask(real * mask)
        discriminator_mask_real_loss = \
            self.get_loss(discriminator_mask_real_decision, self.criterion_GAN, torch.ones)
        discriminator_mask_fake_decision = \
            discriminator_mask(fake * mask)
        discriminator_mask_fake_loss = \
            self.get_loss(discriminator_mask_fake_decision, self.criterion_GAN, torch.zeros)

        return discriminator_mask_real_loss + discriminator_mask_fake_loss

    # get total loss for given generator and discriminator pair, and prepare explainer
    def get_total_gen_loss_and_prep_explainer(self, real: TensorType, mask: TensorType,
                                              generator: Generator,
                                              discriminator: Discriminator,
                                              explainer: ExplanationController) -> torch.Tensor:
        fake = generator(real, mask)
        disc_fake = discriminator(fake)
        disc_fake_mask = self.discriminator_p63_mask(fake * mask)
        generator_loss = self.get_loss(disc_fake, self.criterion_GAN, torch.ones)
        generator_mask_loss = self.get_loss(disc_fake_mask, self.criterion_GAN, torch.ones)

        explainer.set_explanation(fake)
        explainer.set_explanation_m(fake * mask)

        return (self.settings.lambda_mask_adversarial_ratio * generator_mask_loss
                + (1 - self.settings.lambda_mask_adversarial_ratio)
                * generator_loss) * self.settings.lambda_adversarial

    # get total loss for cycle consistency
    def get_total_cycle_loss(self, cycled: TensorType, other_mask: TensorType,
                             other_mask_inverted: TensorType, other_real: TensorType) -> torch.Tensor:
        pixel_wise_cycle_loss = self.criterion_pixel_wise(cycled * other_mask, other_real * other_mask)
        pixel_wise_cycle_loss_inv = self.criterion_pixel_wise(
            cycled * other_mask_inverted, other_real * other_mask_inverted)
        pixel_wise_cycle_loss = pixel_wise_cycle_loss * self.settings.lambda_mask_cycle_ratio
        pixel_wise_cycle_loss_inv = pixel_wise_cycle_loss_inv * (1 - self.settings.lambda_mask_cycle_ratio)

        return pixel_wise_cycle_loss + pixel_wise_cycle_loss_inv

    # get partial discriminator loss
    def get_partial_disc_loss(self, real: TensorType, fake: TensorType,
                              discriminator: Discriminator,
                              coefficient: float,
                              pool: ImagePool = None) -> torch.Tensor:
        discriminator_real_decision = discriminator(real)
        discriminator_real_loss = self.get_loss(discriminator_real_decision, self.criterion_GAN, torch.ones)

        if pool is not None:
            fake = pool.query(fake)

        discriminator_fake_decision = discriminator(fake)
        discriminator_fake_loss = self.get_loss(discriminator_fake_decision, self.criterion_GAN, torch.zeros)

        return (discriminator_real_loss + discriminator_fake_loss) * 0.5 * coefficient

    # training step
    def training_step(self, real_he: TensorType, real_p63: TensorType):
        min_dim = min(real_he.size(0), real_p63.size(0))
        real_he = real_he[:min_dim]
        real_p63 = real_p63[:min_dim]

        (real_he, mask_he), (real_p63, mask_p63) = self.get_dummies(real_he, real_p63)

        # cast to bfloat16 for forward pass, it's faster
        with torch.autocast(device_type="cuda", dtype=self.half_precision):
            fake_p63 = self.generator_he_to_p63(real_he, mask_he)
            cycled_he = self.generator_p63_to_he(fake_p63, mask_he)

            encoded_he_in_he_to_p63 = self.generator_he_to_p63.enc4
            converted_fp63_in_p63_to_he = self.generator_p63_to_he.res_out
            converted_he_in_he_to_p63 = self.generator_he_to_p63.res_out
            encoded_fp63_in_p63_to_he = self.generator_p63_to_he.enc4

            fake_he = self.generator_p63_to_he(real_p63, mask_p63)
            cycled_p63 = self.generator_he_to_p63(fake_he, mask_p63)
            
            encoded_p63_in_p63_to_he = self.generator_p63_to_he.enc4
            converted_fhe_in_he_to_p63 = self.generator_he_to_p63.res_out
            converted_p63_in_p63_to_he = self.generator_p63_to_he.res_out
            encoded_fhe_in_he_to_p63 = self.generator_he_to_p63.enc4

            # set explanations
            self.p63_explainer.set_explanation_m(fake_p63 * mask_he)
            self.he_explainer.set_explanation_m(fake_he * mask_p63)

            # using no grad here due to doubling gradients... explainer automatically resets gradients
            with torch.no_grad():
                discriminator_he_mask_loss = \
                    self.get_total_mask_disc_loss(real_he, mask_he, fake_he, self.discriminator_he_mask) * 0.5
                discriminator_p63_mask_loss = \
                    self.get_total_mask_disc_loss(real_p63, mask_p63, fake_p63, self.discriminator_p63_mask) * 0.5

                mask_he = mask_he + self.p63_explainer.explanation_mask * self.p63_explainer.get_coefficient_mask(
                    discriminator_p63_mask_loss)  # maybe mask_he + mask_he * rest
                mask_p63 = mask_p63 + self.he_explainer.explanation_mask * self.he_explainer.get_coefficient_mask(
                    discriminator_he_mask_loss)

            he_mask_inverted = 1 - mask_he
            p63_mask_inverted = 1 - mask_p63
            he_mask_inverted: TensorType = Variable(he_mask_inverted.to(self.device).to(memory_format=torch.channels_last))
            p63_mask_inverted: TensorType = Variable(p63_mask_inverted.to(self.device).to(memory_format=torch.channels_last))

            # Train generator G
            # A -> B
            generator_he_to_p63_total_loss = self.get_total_gen_loss_and_prep_explainer(real_he,
                                                                                        mask_he,
                                                                                        self.generator_he_to_p63,
                                                                                        self.discriminator_p63,
                                                                                        self.p63_explainer)

            # forward cycle loss
            cycle_he_loss_total = self.get_total_cycle_loss(cycled_he, mask_he, he_mask_inverted, real_he)

            # B -> A
            generator_p63_to_he_total_loss = self.get_total_gen_loss_and_prep_explainer(real_p63,
                                                                                        mask_p63,
                                                                                        self.generator_p63_to_he,
                                                                                        self.discriminator_he,
                                                                                        self.he_explainer)

            # backward cycle loss
            cycle_p63_loss_total = self.get_total_cycle_loss(cycled_p63, mask_p63, p63_mask_inverted, real_p63)

            # total cycle loss
            cycle_loss = (cycle_he_loss_total + cycle_p63_loss_total) * self.settings.lambda_cycle

            # identity loss
            identity_he = self.criterion_pixel_wise(real_he, self.generator_p63_to_he(real_he, mask_he))

            identity_p63 = self.criterion_pixel_wise(real_p63, self.generator_he_to_p63(real_p63, mask_p63))

            identity_loss = (identity_he + identity_p63) * self.settings.lambda_identity

            context_loss = torch.nn.functional.huber_loss(encoded_he_in_he_to_p63, converted_fp63_in_p63_to_he) + \
                torch.nn.functional.huber_loss(converted_he_in_he_to_p63, encoded_fp63_in_p63_to_he)

            context_loss /= 2
            context_loss *= self.settings.lambda_context

            cycle_context_loss = torch.nn.functional.huber_loss(encoded_p63_in_p63_to_he, converted_fhe_in_he_to_p63) + \
                torch.nn.functional.huber_loss(converted_p63_in_p63_to_he, encoded_fhe_in_he_to_p63)

            cycle_context_loss /= 2
            cycle_context_loss *= self.settings.lambda_cycle_context

            # using no grad here due to doubling gradients... explainer automatically resets gradients
            with torch.no_grad():
                discriminator_he_loss_partial = self.get_partial_disc_loss(real_he, fake_he,
                                                                           self.discriminator_he,
                                                                           1 - self.settings.
                                                                           lambda_mask_adversarial_ratio)

                discriminator_he_loss_mask_partial = self.get_partial_disc_loss(real_he * mask_he, fake_he * mask_p63,
                                                                                self.discriminator_he_mask,
                                                                                self.settings.
                                                                                lambda_mask_adversarial_ratio)

                discriminator_p63_loss_partial = self.get_partial_disc_loss(real_p63, fake_p63,
                                                                            self.discriminator_p63,
                                                                            1 - self.settings.
                                                                            lambda_mask_adversarial_ratio)

                discriminator_p63_loss_mask_partial = self.get_partial_disc_loss(real_p63 * mask_p63,
                                                                                 fake_p63 * mask_he,
                                                                                 self.discriminator_p63_mask,
                                                                                 self.settings.
                                                                                 lambda_mask_adversarial_ratio)

                self.p63_explainer.set_losses(discriminator_he_loss_partial, discriminator_he_loss_mask_partial)
                self.he_explainer.set_losses(discriminator_p63_loss_partial, discriminator_p63_loss_mask_partial)
                self.p63_explainer.get_explanation()
                self.he_explainer.get_explanation()

            generator_he_to_p63_total_loss = torch.nan_to_num(generator_he_to_p63_total_loss, nan=0, posinf=1, neginf=-1)
            generator_p63_to_he_total_loss = torch.nan_to_num(generator_p63_to_he_total_loss, nan=0, posinf=1, neginf=-1)
            cycle_loss = torch.nan_to_num(cycle_loss, nan=0, posinf=1, neginf=-1)
            identity_loss = torch.nan_to_num(identity_loss, nan=0, posinf=1, neginf=-1)

            # backward gen
            generator_loss = \
                + generator_he_to_p63_total_loss \
                + generator_p63_to_he_total_loss \
                + cycle_loss \
                + identity_loss \
                + context_loss \
                + cycle_context_loss

        self.generator_optimizer.zero_grad(set_to_none=True)
        generator_loss.backward()

        for param_he_to_p63, param_p63_to_he in zip(self.generator_he_to_p63.parameters(), self.generator_p63_to_he.parameters()):
            param_he_to_p63.grad.data.clamp(-1, 1)
            param_p63_to_he.grad.data.clamp(-1, 1)

        self.generator_optimizer.step()

        # Back propagation for discriminators
        with torch.autocast(device_type="cuda", dtype=self.half_precision):
            discriminator_he_loss_partial = self.get_partial_disc_loss(real_he, fake_he, self.discriminator_he,
                                                                       1 - self.settings.lambda_mask_adversarial_ratio,
                                                                       self.fake_he_pool)

            discriminator_he_loss_mask_partial = self.get_partial_disc_loss(real_he * mask_he, fake_he * mask_p63,
                                                                            self.discriminator_he_mask,
                                                                            self.settings.lambda_mask_adversarial_ratio,
                                                                            self.fake_he_pool)

            discriminator_he_loss = discriminator_he_loss_partial + discriminator_he_loss_mask_partial

        self.discriminator_he_optimizer.zero_grad(set_to_none=True)
        discriminator_he_loss = torch.nan_to_num(discriminator_he_loss, nan=0, posinf=1, neginf=0)
        discriminator_he_loss.backward()

        for param_disc_he, param_disc_mask_he in zip(self.discriminator_he.parameters(), self.discriminator_he_mask.parameters()):
            param_disc_he.grad.data.clamp(-1, 1)
            param_disc_mask_he.grad.data.clamp(-1, 1)

        self.discriminator_he_optimizer.step()

        with torch.autocast(device_type="cuda", dtype=self.half_precision):
            discriminator_p63_loss_partial = self.get_partial_disc_loss(real_p63, fake_p63, self.discriminator_p63,
                                                                        1 - self.settings.lambda_mask_adversarial_ratio,
                                                                        self.fake_p63_pool)

            discriminator_p63_loss_mask_partial = self.get_partial_disc_loss(real_p63 * mask_p63, fake_p63 * mask_he,
                                                                             self.discriminator_p63_mask,
                                                                             self.settings.lambda_mask_adversarial_ratio,
                                                                             self.fake_p63_pool)

            discriminator_p63_loss = discriminator_p63_loss_partial + discriminator_p63_loss_mask_partial

        self.discriminator_p63_optimizer.zero_grad(set_to_none=True)
        discriminator_p63_loss = torch.nan_to_num(discriminator_p63_loss, nan=0, posinf=1, neginf=-1)
        discriminator_p63_loss.backward()

        for param_disc_p63, param_disc_mask_p63 in zip(self.discriminator_p63.parameters(), self.discriminator_p63_mask.parameters()):
            param_disc_p63.grad.data.clamp(-1, 1)
            param_disc_mask_p63.grad.data.clamp(-1, 1)

        self.discriminator_p63_optimizer.step()

        # logging losses
        self.latest_generator_loss = generator_loss.item()
        self.latest_discriminator_he_loss = discriminator_he_loss.item()
        self.latest_discriminator_p63_loss = discriminator_p63_loss.item()
        self.latest_identity_loss = identity_loss.item()
        self.latest_cycle_loss = cycle_loss.item()
        self.latest_context_loss = context_loss.item()
        self.latest_cycle_context_loss = cycle_context_loss.item()

        if torch.multiprocessing.current_process().name == 'MainProcess':
            self.wandb_module.discriminator_he_running_loss_avg.append(discriminator_he_loss.item())
            self.wandb_module.discriminator_p63_running_loss_avg.append(discriminator_p63_loss.item())
            self.wandb_module.generator_he_to_p63_running_loss_avg.append(generator_he_to_p63_total_loss.item())
            self.wandb_module.generator_p63_to_he_running_loss_avg.append(generator_p63_to_he_total_loss.item())
            self.wandb_module.cycle_he_running_loss_avg.append(cycle_loss.item())
            self.wandb_module.cycle_p63_running_loss_avg.append(cycle_loss.item())
            self.wandb_module.total_running_loss_avg.append(generator_loss.item())
            self.wandb_module.context_running_loss_avg.append(context_loss.item())
            self.wandb_module.cycle_context_running_loss_avg.append(cycle_context_loss.item())

    # evaluation step
    def get_image_pairs(self):
        real_he = self.test_he_data.get_sequential_image()
        real_p63 = self.test_p63_data.get_sequential_image()

        real_he = Variable(real_he.to(self.device)).expand(1, -1, -1, -1).to(memory_format=torch.channels_last)
        real_p63 = Variable(real_p63.to(self.device)).expand(1, -1, -1, -1).to(memory_format=torch.channels_last)

        real_he_mask = get_mask(real_he, self.settings.mask_type)
        real_p63_mask = get_mask(real_p63, self.settings.mask_type)

        real_he_mask = Variable(real_he_mask.to(self.device).to(memory_format=torch.channels_last))
        real_p63_mask = Variable(real_p63_mask.to(self.device).to(memory_format=torch.channels_last))

        fake_p63 = self.generator_he_to_p63(real_he, real_he_mask)
        reconstructed_he = self.generator_p63_to_he(fake_p63, real_he_mask)

        fake_he = self.generator_p63_to_he(real_p63, real_p63_mask)
        reconstructed_p63 = self.generator_he_to_p63(fake_he, real_p63_mask)

        return (real_he, real_p63), (fake_he, fake_p63), (reconstructed_he, reconstructed_p63)

    def get_image_pairs_paired(self):
        real_he = self.test_he_data.get_sequential_image2()
        real_p63 = self.test_p63_data.get_sequential_image2()

        real_he = Variable(real_he.to(self.device)).expand(1, -1, -1, -1).to(memory_format=torch.channels_last)
        real_p63 = Variable(real_p63.to(self.device)).expand(1, -1, -1, -1).to(memory_format=torch.channels_last)

        real_he_mask = get_mask(real_he, self.settings.mask_type)
        real_p63_mask = get_mask(real_p63, self.settings.mask_type)

        real_he_mask = Variable(real_he_mask.to(self.device).to(memory_format=torch.channels_last))
        real_p63_mask = Variable(real_p63_mask.to(self.device).to(memory_format=torch.channels_last))

        fake_p63 = self.generator_he_to_p63(real_he, real_he_mask)
        reconstructed_he = self.generator_p63_to_he(fake_p63, real_he_mask)

        fake_he = self.generator_p63_to_he(real_p63, real_p63_mask)
        reconstructed_p63 = self.generator_he_to_p63(fake_he, real_p63_mask)

        return (real_he, real_p63), (fake_he, fake_p63), (reconstructed_he, reconstructed_p63)

    def get_dummies(self, real_he, real_p63) -> tuple[tuple[TensorType, TensorType], tuple[TensorType, TensorType]]:
        real_he = Variable(real_he.to(self.device).to(memory_format=torch.channels_last))
        real_p63 = Variable(real_p63.to(self.device).to(memory_format=torch.channels_last))
        mask_he = get_mask(real_he, self.settings.mask_type)
        mask_p63 = get_mask(real_p63, self.settings.mask_type)
        mask_he = Variable(mask_he.to(self.device).to(memory_format=torch.channels_last))
        mask_p63 = Variable(mask_p63.to(self.device).to(memory_format=torch.channels_last))

        return (real_he, mask_he), (real_p63, mask_p63)

    def eval_step(self, real_he: TensorType, real_p63: TensorType):
        (real_he, mask_he), (real_p63, mask_p63) = self.get_dummies(real_he, real_p63)

        fake_p63 = self.generator_he_to_p63(real_he, mask_he)
        fake_he = self.generator_p63_to_he(real_p63, mask_p63)
        cycled_he = self.generator_p63_to_he(fake_p63, mask_he)
        cycled_p63 = self.generator_he_to_p63(fake_he, mask_p63)

        return fake_he, cycled_he, fake_p63, cycled_p63
