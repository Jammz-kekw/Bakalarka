"""
    Prevzatý kód
"""

import torch
import torch.nn.functional as F
import kornia
from typing import Tuple, Union

from kornia.core.check import KORNIA_CHECK_SHAPE
from kornia.filters.kernels import _unpack_2d_ks, get_gaussian_kernel2d
from kornia.filters.median import _compute_zero_padding
from kornia.core import Tensor, pad

# calculated range for LAB values based on custom normalization, used as activation function parameters
# and for tang correction
A_AB = -1.72879524581
B_AB = 1.71528903296
A_L = -1.68976005407
B_L = 1.68976005407


TensorType = Union[torch.Tensor, torch.autograd.Variable]


# This function is from a new versions of Kornia that is not yet released and only in C++,
# so I copied it here and modified it to work with the current version of Kornia in Python
def joint_bilateral_blur(
    inp: Tensor,
    guidance: Union[Tensor, None],
    kernel_size: Union[Tuple[int, int], int],
    sigma_color: Union[float, Tensor],
    sigma_space: Union[Tuple[float, float], Tensor],
    border_type: str = 'reflect',
    color_distance_type: str = 'l1',
) -> Tensor:
    if isinstance(sigma_color, Tensor):
        KORNIA_CHECK_SHAPE(sigma_color, ['B'])
        sigma_color = sigma_color.to(device=inp.device, dtype=inp.dtype).view(-1, 1, 1, 1, 1)

    kx, ky = _unpack_2d_ks(kernel_size)
    pad_x, pad_y = _compute_zero_padding(kernel_size)

    padded_input = pad(inp, (pad_x, pad_x, pad_y, pad_y), mode=border_type)
    unfolded_input = padded_input.unfold(2, ky, 1).unfold(3, kx, 1).flatten(-2)  # (B, C, H, W, K x K)

    if guidance is None:
        guidance = inp
        unfolded_guidance = unfolded_input
    else:
        padded_guidance = pad(guidance, (pad_x, pad_x, pad_y, pad_y), mode=border_type)
        unfolded_guidance = padded_guidance.unfold(2, ky, 1).unfold(3, kx, 1).flatten(-2)  # (B, C, H, W, K x K)

    diff = unfolded_guidance - guidance.unsqueeze(-1)
    if color_distance_type == "l1":
        color_distance_sq = diff.abs().sum(1, keepdim=True).square()
    elif color_distance_type == "l2":
        color_distance_sq = diff.square().sum(1, keepdim=True)
    else:
        raise ValueError("color_distance_type only acceps l1 or l2")
    color_kernel = (-0.5 / sigma_color**2 * color_distance_sq).exp()  # (B, 1, H, W, K x K)

    space_kernel = get_gaussian_kernel2d(kernel_size, sigma_space, device=inp.device, dtype=inp.dtype)
    space_kernel = space_kernel.view(-1, 1, 1, 1, kx * ky)

    kernel = space_kernel * color_kernel
    out = (unfolded_input * kernel).sum(-1) / kernel.sum(-1)
    return out


# correct the output of the network to be in the range of LAB values
class TanhCorrection(torch.nn.Module):

    def __init__(self, steepness=4):
        super(TanhCorrection, self).__init__()
        self.lumi_offset = torch.nn.Parameter(torch.tensor([1.]))
        self.steepness = steepness

    def steep_sig(self, x):
        return 1 / (1 + torch.exp(-self.steepness * x))

    def forward(self, x: TensorType) -> torch.Tensor:
        x_l = x[:, 0:1, :, :]
        x_ab = x[:, 1:, :, :]
        x_l = (B_L - A_L) * (x_l + 1) / 2 + A_L
        x_ab = (B_AB - A_AB) * (x_ab + 1) / 2 + A_AB
        return torch.cat((x_l * self.steep_sig(self.lumi_offset), x_ab), dim=1)


class ConvolutionalSelfAttention(torch.nn.Module):

    def __init__(self, n_channels, reduction=8):
        super(ConvolutionalSelfAttention, self).__init__()
        (self.query,
         self.key,
         self.value) = [self._conv(n_channels, c) for c in (n_channels//reduction, n_channels//reduction, n_channels)]
        self.gamma = torch.nn.Parameter(torch.tensor([0.]))

    def _conv(self, n_in, n_out):
        return torch.nn.utils.spectral_norm(torch.nn.Conv1d(n_in, n_out, kernel_size=1, bias=False))

    def forward(self, skip, res):
        size = skip.size()
        x_skip = skip.view(*size[:2], -1)
        x_res = res.view(*size[:2], -1)

        f, g, h = self.query(x_skip), self.key(x_skip), self.value(x_res)

        beta = F.softmax(torch.bmm(f.transpose(1, 2), g), dim=1)
        o = self.gamma * torch.bmm(h, beta) + x_skip
        o = o.view(*size)
        return o


class ConvBlock(torch.nn.Module):
    def __init__(self, input_size, output_size, kernel_size=3, stride=2, padding=1, activation='relu', batch_norm=True):
        super(ConvBlock, self).__init__()
        self.conv = torch.nn.Conv2d(input_size, output_size, kernel_size, stride, padding)
        self.batch_norm = batch_norm
        self.bn = torch.nn.InstanceNorm2d(output_size)
        self.activation = activation
        self.relu = torch.nn.ReLU(inplace=True)
        self.lrelu = torch.nn.LeakyReLU(0.2, inplace=True)
        self.tanh = torch.nn.Tanh()

    def forward(self, x):
        if self.batch_norm:
            out = self.bn(self.conv(x))
        else:
            out = self.conv(x)

        if self.activation == 'relu':
            return self.relu(out)
        elif self.activation == 'lrelu':
            return self.lrelu(out)
        elif self.activation == 'tanh':
            return self.tanh(out)
        elif self.activation == 'no_act':
            return out


class DeconvBlock(torch.nn.Module):
    def __init__(self, input_size, output_size, kernel_size=3, stride=2, padding=1, output_padding=1, batch_norm=True):
        super(DeconvBlock, self).__init__()
        self.deconv = torch.nn.ConvTranspose2d(input_size, output_size, kernel_size, stride, padding, output_padding)
        self.batch_norm = batch_norm
        self.bn = torch.nn.InstanceNorm2d(output_size)
        self.elu = torch.nn.ELU(A_AB)

    def forward(self, x):
        if self.batch_norm:
            out = self.bn(self.deconv(x))
        else:
            out = self.deconv(x)

        return self.elu(out)


class ResnetBlock(torch.nn.Module):
    def __init__(self, num_filter, kernel_size=3, stride=1, padding=0):
        super(ResnetBlock, self).__init__()
        conv1 = torch.nn.Conv2d(num_filter, num_filter, kernel_size, stride, padding)
        conv2 = torch.nn.Conv2d(num_filter, num_filter, kernel_size, stride, padding)
        bn = torch.nn.InstanceNorm2d(num_filter)
        gelu = torch.nn.GELU()
        pad = torch.nn.ReflectionPad2d(1)

        self.resnet_block = torch.nn.Sequential(
            pad,
            conv1,
            bn,
            gelu,
            pad,
            conv2,
            bn
        )

    def forward(self, x):
        return self.resnet_block(x) + x


class Generator(torch.nn.Module):
    def __init__(self, num_filter, num_resnet, input_dim=3, output_dim=3):
        super(Generator, self).__init__()

        # Mask encoder
        self.conv1dc = ConvBlock(input_dim * 2, input_dim, kernel_size=1, stride=1, padding=0, activation='no_act', batch_norm=False)
        self.conv1dm = ConvBlock(input_dim * 2, input_dim, kernel_size=1, stride=1, padding=0, activation='no_act', batch_norm=False)
        
        self.interpretable_conv_1 = ConvBlock(input_dim, num_filter//2, kernel_size=1, stride=1, padding=0)
        self.interpretable_conv_2 = ConvBlock(num_filter//2, num_filter//2, kernel_size=1, stride=1, padding=0)

        # Reflection padding
        self.pad = torch.nn.ReflectionPad2d(3)
        self.pad1 = torch.nn.ReflectionPad2d(1)

        # Encoder
        self.conv1 = ConvBlock(num_filter//2, num_filter, kernel_size=7, stride=1, padding=0)
        self.conv2 = ConvBlock(num_filter, num_filter * 2)
        num_filter *= 2
        self.conv3 = ConvBlock(num_filter, num_filter * 2)
        num_filter *= 2
        self.conv4 = ConvBlock(num_filter, num_filter * 2)
        num_filter *= 2

        # Resnet blocks
        self.resnet_blocks = []
        for i in range(num_resnet):
            self.resnet_blocks.append(ResnetBlock(num_filter))

        self.resnet_blocks = torch.nn.Sequential(*self.resnet_blocks)

        # Decoder
        self.deconv1 = DeconvBlock(num_filter, num_filter // 2)
        self.attention1 = ConvolutionalSelfAttention(num_filter, 64)
        num_filter //= 2
        self.deconv2 = DeconvBlock(num_filter, num_filter // 2)
        self.attention2 = ConvolutionalSelfAttention(num_filter, 32)
        num_filter //= 2
        self.deconv3 = DeconvBlock(num_filter, num_filter // 2)
        #self.attention3 = ConvolutionalSelfAttention(num_filter, 16)
        num_filter //= 2
        self.deconv4 = ConvBlock(num_filter, num_filter, kernel_size=7, stride=1, padding=0)
        #self.attention4 = ConvolutionalSelfAttention(num_filter, 16)
        self.correction = ConvBlock(num_filter, num_filter, kernel_size=3, stride=1, padding=0,
                                    activation='no_act', batch_norm=False)
        self.final = ConvBlock(num_filter, output_dim, kernel_size=3, stride=1, padding=0,
                               activation='tanh', batch_norm=False)

        self.unsharp_filter = kornia.filters.UnsharpMask((5, 5), (1.5, 1.5))
        self.guided_blur = lambda inp, gui: joint_bilateral_blur(inp, gui, (5, 5), 0.1, (1.5, 1.5))
        self.tanh_corr = TanhCorrection()

        self.enc4 = None
        self.res_out = None
        
    def forward(self, img, mask=None):
        # Mask encoder
        if mask is not None:
            inv_masked_img = torch.cat(((1 - mask) * img, (1 - mask).expand(img.size(0), -1, -1, -1)), 1)  # context
            imgx = torch.cat((mask*img, mask.expand(img.size(0), -1, -1, -1)), 1)  # mask

            imgx = self.conv1dc(imgx)
            inv_masked_img = self.conv1dm(inv_masked_img)

            imgx = self.interpretable_conv_1(imgx)
            imgx = self.interpretable_conv_2(imgx)
        else:
            imgx = self.interpretable_conv_2(self.interpretable_conv_1(img))

        enc1 = self.conv1(self.pad(imgx))  # (bs, num_filter, 128, 128)
        enc2 = self.conv2(enc1)  # (bs, num_filter * 2, 64, 64)
        enc3 = self.conv3(enc2)  # (bs, num_filter * 4, 32, 32)
        self.enc4 = self.conv4(enc3)  # (bs, num_filter * 8, 16, 16)

        # Resnet blocks
        res = self.resnet_blocks(self.enc4)
        self.res_out = res

        # Decoder
        dec1 = self.deconv1(self.attention1(self.enc4, res))
        dec2 = self.deconv2(self.attention2(dec1, enc3))
        dec3 = self.deconv3(dec2 + enc2)
        dec4 = self.deconv4(self.pad(dec3 + enc1))
        out = self.correction(self.pad1(dec4))
        out = self.final(self.pad1(out))

        out = self.tanh_corr(out)

        if mask is not None:
            # noinspection PyUnboundLocalVariable
            out = out + inv_masked_img

        out = self.guided_blur(out, img)
        out = self.unsharp_filter(out)

        return out

    def normal_weight_init(self, mean=0.0, std=0.02):
        for m in self.children():
            if isinstance(m, ConvBlock):
                torch.nn.init.normal_(m.conv.weight, mean, std)
            if isinstance(m, DeconvBlock):
                torch.nn.init.normal_(m.deconv.weight, mean, std)
            if isinstance(m, ResnetBlock):
                torch.nn.init.normal_(m.conv.weight, mean, std)
                torch.nn.init.constant(m.conv.bias, 0)

    def get_partial_pass(self, img, mask):
        inv_masked_img = torch.cat(((1 - mask) * img, (1 - mask).expand(img.size(0), -1, -1, -1)), 1)
        imgx = torch.cat((mask * img, mask.expand(img.size(0), -1, -1, -1)), 1)
        imgx = self.conv1dc(imgx)
        inv_masked_img = self.conv1dm(inv_masked_img)
        imgx = self.interpretable_conv_1(imgx)
        imgx = self.interpretable_conv_2(imgx)

        return imgx, inv_masked_img

    def get_modified_rest_pass(self, original, codes, mask_codes, eigen):  # very inefficient way to do this, better to have a func (but we were lazy)
        new_codes = codes.clone()
        new_codes = torch.einsum('lkji,nk->nkji', new_codes, eigen).sum(0, keepdim=True).add(new_codes)

        enc1 = self.conv1(self.pad(new_codes))
        enc2 = self.conv2(enc1)
        enc3 = self.conv3(enc2)
        enc4 = self.conv4(enc3)
        img = self.resnet_blocks(enc4)
        img = self.deconv1(self.attention1(enc4, img))
        img = self.deconv2(self.attention2(img, enc3))
        img = self.deconv3(img + enc2)
        img = self.deconv4(self.pad(img + enc1))
        img = self.correction(self.pad1(img))
        img = self.final(self.pad1(img))
        img = self.tanh_corr(img)
        img = img + mask_codes
        img = self.guided_blur(img, original)
        img = self.unsharp_filter(img)
        return img

    def get_encoded(self):
        return self.enc4

    def get_resnet_transformed(self):
        return self.res_out


class Discriminator(torch.nn.Module):
    def __init__(self, num_filter, input_dim=3, output_dim=1):
        super(Discriminator, self).__init__()

        conv1 = ConvBlock(input_dim, num_filter, kernel_size=4, stride=2, padding=1, activation='lrelu',
                          batch_norm=False)
        conv2 = ConvBlock(num_filter, num_filter * 2, kernel_size=4, stride=2, padding=1, activation='lrelu')
        conv3 = ConvBlock(num_filter * 2, num_filter * 4, kernel_size=4, stride=2, padding=1, activation='lrelu')
        conv4 = ConvBlock(num_filter * 4, num_filter * 8, kernel_size=4, stride=1, padding=1, activation='lrelu')
        self.conv5 = ConvBlock(num_filter * 8, output_dim, kernel_size=4, stride=1, padding=1, activation='no_act',
                               batch_norm=False)

        self.conv_blocks = torch.nn.Sequential(
            conv1,
            conv2,
            conv3,
            conv4,
            self.conv5
        )

    def forward(self, x):
        out = self.conv_blocks(x)
        return out

    def loss_fake(self, x):  # a modified forward pass compatible with captum explanations
        out = self.forward(x)
        out = torch.nn.functional.adaptive_max_pool2d(out, output_size=1).squeeze(0).squeeze(0)
        return out

    # deprecated
    def normal_weight_init(self, mean=0.0, std=0.02):  # switched to default pytorch init
        for m in self.children():
            if isinstance(m, ConvBlock):
                torch.nn.init.normal(m.conv.weight, mean, std)
