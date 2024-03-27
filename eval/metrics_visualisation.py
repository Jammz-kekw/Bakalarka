import os
import numpy as np
import matplotlib.pyplot as plt
import cv2


def compute_values(original_image, generated_image):
    original_patches = split_into_regions(original_image)
    generated_patches = split_into_regions(generated_image)

    l_values_bha = []
    a_values_bha = []
    b_values_bha = []

    l_values_cor = []
    a_values_cor = []
    b_values_cor = []

    for orig_patch, gen_patch in zip(original_patches, generated_patches):
        l_coefficient, a_coefficient, b_coefficient = get_bhattacharyya(orig_patch, gen_patch)

        l_values_bha.append(l_coefficient)
        a_values_bha.append(a_coefficient)
        b_values_bha.append(b_coefficient)

        l_correlation, a_correlation, b_correlation = get_correlation(orig_patch, gen_patch)

        l_values_cor.append(l_correlation)
        a_values_cor.append(a_correlation)
        b_values_cor.append(b_correlation)

    return [l_values_bha, a_values_bha, b_values_bha], [l_values_cor, a_values_cor, b_values_cor]


def visualise_bhattacharyya(original, generated, normalized, generated_lab, normalized_lab, tag, metric):
    """
            |  orig  |  generated  |  normalized  |
            |--------|-------------|--------------|
            |    x   |      l      |       l      |
            |    x   |      a      |       a      |
            |    x   |      b      |       b      |

            4x3
    """

    if metric == 'bha':
        title = "Bhattacharyya"
        color = 'RdYlGn_r'
    else:
        title = "Correlation"
        color = 'RdYlGn'

    stain = tag.split('-')[0].strip()

    gen_l, gen_a, gen_b = generated_lab
    norm_l, norm_a, norm_b = normalized_lab

    grid_gen_l = np.array(gen_l).reshape((4, 4))
    grid_gen_a = np.array(gen_a).reshape((4, 4))
    grid_gen_b = np.array(gen_b).reshape((4, 4))

    grid_norm_l = np.array(norm_l).reshape((4, 4))
    grid_norm_a = np.array(norm_a).reshape((4, 4))
    grid_norm_b = np.array(norm_b).reshape((4, 4))

    plt.figure(figsize=(20, 14))

    # Original image
    plt.subplot(4, 3, 1)
    plt.imshow(original)
    plt.title(f"Pôvodný {stain}")
    plt.axis('off')

    # Generated image
    plt.subplot(4, 3, 2)
    plt.imshow(generated)
    plt.title(f"Generovaný {stain}")
    plt.axis('off')

    # Normalized image
    plt.subplot(4, 3, 3)
    plt.imshow(normalized)
    plt.title(f"Normalizovaný {stain}")
    plt.axis('off')

    # Generated L
    plt.subplot(4, 3, 5)
    plt.imshow(grid_gen_l, cmap=color, interpolation='nearest', vmin=0, vmax=1)
    plt.title("Generovaný L")
    plt.axis('off')

    for i in range(grid_gen_l.shape[0]):
        for j in range(grid_gen_l.shape[1]):
            value = f'{grid_gen_l[i, j]:.2f}'
            plt.text(j, i, value, ha='center', va='center', color='black')

    # Generated A
    plt.subplot(4, 3, 8)
    plt.imshow(grid_gen_a, cmap=color, interpolation='nearest', vmin=0, vmax=1)
    plt.title("Generovaný A")
    plt.axis('off')

    for i in range(grid_gen_a.shape[0]):
        for j in range(grid_gen_a.shape[1]):
            value = f'{grid_gen_a[i, j]:.2f}'
            plt.text(j, i, value, ha='center', va='center', color='black')

    # Generated B
    plt.subplot(4, 3, 11)
    plt.imshow(grid_gen_a, cmap=color, interpolation='nearest', vmin=0, vmax=1)
    plt.title("Generovaný B")
    plt.axis('off')

    for i in range(grid_gen_b.shape[0]):
        for j in range(grid_gen_b.shape[1]):
            value = f'{grid_gen_b[i, j]:.2f}'
            plt.text(j, i, value, ha='center', va='center', color='black')

    plt.suptitle(title + " | " + tag)
    plt.show()



def visualise_correlation(original_image, generated_image):
    pass


def split_into_regions(image):
    patch_size = 64

    patches = []
    height, width = image.shape[:2]

    for i in range(0, height, patch_size):
        for j in range(0, width, patch_size):
            patch = image[i:i + patch_size, j:j + patch_size]
            patches.append(patch)

    return patches


def l_channel_normalization(original_lab, generated_lab):
    original_l = original_lab[:, :, 0]
    generated_l = generated_lab[:, :, 0]

    original_mean, original_std = np.mean(original_l), np.std(original_l)
    generated_mean, generated_std = np.mean(generated_l), np.std(generated_l)

    normalized_l = (generated_l - generated_mean) * (original_std / generated_std) + original_mean
    normalized_l = np.clip(normalized_l, 0, 255)  # Just to avoid values out of 8-bit space

    generated_lab[:, :, 0] = normalized_l

    normalized_rgb = cv2.cvtColor(generated_lab, cv2.COLOR_LAB2BGR)

    return normalized_rgb


def get_channels(patch):
    l_channel = cv2.calcHist([patch], [0], None, [256], [0, 256])
    a_channel = cv2.calcHist([patch], [1], None, [256], [0, 256])
    b_channel = cv2.calcHist([patch], [2], None, [256], [0, 256])

    l_channel /= l_channel.sum()
    a_channel /= a_channel.sum()
    b_channel /= b_channel.sum()

    return l_channel, a_channel, b_channel


def get_bhattacharyya(original, generated):
    l_orig, a_orig, b_orig = get_channels(original)
    l_gen, a_gen, b_gen = get_channels(generated)

    l_coefficient = cv2.compareHist(l_orig, l_gen, cv2.HISTCMP_BHATTACHARYYA)
    a_coefficient = cv2.compareHist(a_orig, a_gen, cv2.HISTCMP_BHATTACHARYYA)
    b_coefficient = cv2.compareHist(b_orig, b_gen, cv2.HISTCMP_BHATTACHARYYA)

    return l_coefficient, a_coefficient, b_coefficient


def get_correlation(original, generated):
    l_orig, a_orig, b_orig = get_channels(original)
    l_gen, a_gen, b_gen = get_channels(generated)

    l_correlation = cv2.compareHist(l_orig, l_gen, cv2.HISTCMP_CORREL)
    a_correlation = cv2.compareHist(a_orig, a_gen, cv2.HISTCMP_CORREL)
    b_correlation = cv2.compareHist(b_orig, b_gen, cv2.HISTCMP_CORREL)

    return l_correlation, a_correlation, b_correlation


def load_image(name, folder):
    image_path = os.path.join(folder, name)
    image = cv2.imread(image_path)

    return image


def run_pairs(original_files, generated_files, original_path, generated_path, tag):
    for _, (original_image, generated_image) in enumerate(zip(original_files, generated_files)):
        """
            1. rgb -> lab
            2. lab normalization on generated image
            3. calculate bhattacharyya and correlation - thus return list of 16 values
               for each channel  
            4. visualise bhattacharyya as 3x4 using green to red gradient
            5. visualise correlation as 3x4 using yellow to red gradient ? # TODO - try this to see the results nech to each other and then compare them
        """

        image_no = original_image.split('_')[0]
        tag = tag + " - " + image_no

        original_rgb = load_image(original_image, original_path)
        generated_rgb = load_image(generated_image, generated_path)

        original_lab = cv2.cvtColor(original_rgb, cv2.COLOR_BGR2LAB)
        generated_lab = cv2.cvtColor(generated_rgb, cv2.COLOR_BGR2LAB)

        normalized_rgb = l_channel_normalization(original_lab, generated_lab)
        normalized_lab = cv2.cvtColor(normalized_rgb, cv2.COLOR_BGR2LAB)

        generated_bha, generated_cor = compute_values(original_lab, generated_lab)
        normalized_bha, normalized_cor = compute_values(original_lab, normalized_lab)

        visualise_bhattacharyya(original_rgb, generated_rgb, normalized_rgb,
                                generated_bha, normalized_bha, tag, 'bha')

        print("halo")
        break



if __name__ == '__main__':
    orig_he_folder_path = 'D:\FIIT\Bachelor-s-thesis\Dataset\\results_cut\\run_4x\\orig_he'
    ihc_to_he_folder_path = 'D:\FIIT\Bachelor-s-thesis\Dataset\\results_cut\\run_4x\\ihc_to_he'

    orig_ihc_folder_path = 'D:\FIIT\Bachelor-s-thesis\Dataset\\results_cut\\run_4x\\orig_ihc'
    he_to_ihc_folder_path = 'D:\FIIT\Bachelor-s-thesis\Dataset\\results_cut\\run_4x\\he_to_ihc'

    orig_he_files = os.listdir(orig_he_folder_path)
    ihc_to_he_files = os.listdir(ihc_to_he_folder_path)

    orig_ihc_files = os.listdir(orig_ihc_folder_path)
    he_to_ihc_files = os.listdir(he_to_ihc_folder_path)

    run_pairs(orig_he_files, ihc_to_he_files, orig_he_folder_path, ihc_to_he_folder_path, "HE")



