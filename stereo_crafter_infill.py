import argparse
import numpy as np
import os
import torch
import cv2

from transformers import CLIPVisionModelWithProjection
from diffusers import AutoencoderKLTemporalDecoder, UNetSpatioTemporalConditionModel

from StereoCrafter.pipelines.stereo_video_inpainting import StableVideoDiffusionInpaintingPipeline, tensor2vid
from scipy.ndimage import binary_dilation

import cv2

num_inference_steps = 3#More steps look better but is slower
black = np.array([0, 0, 0], dtype=np.uint8)
blue = np.array([0, 0, 255], dtype=np.uint8)
pipeline = None
frame_rate, frame_width, frame_height = None, None, None
def generate_infilled_frames(input_frames, input_masks):
    global num_inference_steps
    input_frames = torch.tensor(input_frames).permute(0, 3, 1, 2).float()/255.0
    frames_mask = torch.tensor(input_masks).permute(0, 1, 2).float()/255.0
    
    video_latents = pipeline(
        frames=input_frames,
        frames_mask=frames_mask,
        height=input_frames.shape[2],
        width=input_frames.shape[3],
        num_frames=len(input_frames),
        output_type="latent",
        min_guidance_scale=1.01,
        max_guidance_scale=1.01,
        decode_chunk_size=8,
        fps=frame_rate,
        motion_bucket_id=127,
        noise_aug_strength=0.0,
        num_inference_steps=num_inference_steps,
    ).frames[0]

    video_latents = video_latents.unsqueeze(0)
    if video_latents == torch.float16:
        pipeline.vae.to(dtype=torch.float16)

    video_frames = pipeline.decode_latents(video_latents, num_frames=video_latents.shape[1], decode_chunk_size=2)
    video_frames = tensor2vid(video_frames, pipeline.image_processor, output_type="np")[0]

    return (video_frames*255).astype(np.uint8)


def mark_lower_side(normals_img, max_steps=30):
    """
    Vectorized version of mark_lower_side using NumPy.
    normals_img: H×W×3 uint8
    max_steps: how far to march before giving up
    """
    H, W = normals_img.shape[:2]
    orig = normals_img  # alias

    # 1) valid normals mask
    valid = ~np.all(orig == 0, axis=-1)

    # 2) extract & normalize dx, dy only for valid pixels
    ys, xs = np.nonzero(valid)
    pts = np.stack([xs, ys], axis=1).astype(np.float32)           # N×2
    dirs = ((orig[ys, xs, :2].astype(np.float32) / 255)*2 - 1)    # N×2 raw
    norms = np.linalg.norm(dirs, axis=1, keepdims=True)
    good = (norms[:,0] > 1e-6)
    pts   = pts[good]
    dirs  = dirs[good] / norms[good]   # N_good×2 unit vectors

    N = pts.shape[0]
    alive = np.ones(N, dtype=bool)
    res_pts = -np.ones((N, 2), dtype=int)  # to store hit positions

    # 3) march all rays in lockstep
    for t in range(1, max_steps):
        # compute new sample positions for all *alive* rays
        idx = np.nonzero(alive)[0]
        if idx.size == 0:
            break

        p = pts[idx] + dirs[idx] * t        # float positions N_alive×2
        xi = np.rint(p[:,0]).astype(int)
        yi = np.rint(p[:,1]).astype(int)

        # which are still in-bounds?
        inb = (xi >= 0) & (xi < W) & (yi >= 0) & (yi < H)

        # for those in-bounds, check if we've hit a black pixel
        xi_in = xi[inb]; yi_in = yi[inb]
        orig_vals = orig[yi_in, xi_in]       # M×3
        bg_hit = np.all(orig_vals == 0, axis=1)

        # mark any that hit background this step
        hit_idx = idx[inb][bg_hit]           # indices in the big array
        if hit_idx.size > 0:
            # step back one to get the "lower side" pixel
            p0 = pts[hit_idx] + dirs[hit_idx] * (t-1)
            xb = np.rint(p0[:,0]).astype(int)
            yb = np.rint(p0[:,1]).astype(int)
            res_pts[hit_idx, 0] = xb
            res_pts[hit_idx, 1] = yb

        # any ray that either went out-of-bounds or hit bg should stop
        idx_oob = idx[~inb]
        alive[idx_oob] = False
        alive[hit_idx] = False

    # 4) scatter the blue marks into the output image
    output = np.zeros_like(orig)
    xb = res_pts[:,0]; yb = res_pts[:,1]
    valid_hits = (xb >= 0) & (yb >= 0)
    output[yb[valid_hits], xb[valid_hits]] = (0, 0, 255)

    return output

def deal_with_frame_chunk(keep_first_three, chunk, out, keep_last_three):

    ##where the side by side picture ends
    pic_width = int(frame_width//2)


    #Looks like shit at 512 x 512 but looks quite good at 1024 x 1024

    #1024x768 looks good enogh a okay tradeof betwen looks and speed
    new_width = 1024
    new_height = 768
    # Default seams to be height: int = 576, width: int = 1024,

    right_input = []
    left_input = []
    
    right_mask_input = []
    left_mask_input = []
    for img_and_mask in chunk:
        
        #Right mask
        org_img_mask = img_and_mask[1][:frame_height, pic_width:]
        img_mask_true_paralax = ~np.all(org_img_mask == black, axis=-1)
        img_mask_resized = np.array(cv2.resize(img_mask_true_paralax.astype(np.uint8)*255, (new_width, new_height)) > 0).astype(np.uint8)*255
        right_mask_input.append(img_mask_resized)
        
        #Right image
        org_img = img_and_mask[0][:frame_height, pic_width:]
        img_resized = cv2.resize(org_img, (new_width, new_height))
        right_input.append(img_resized)
        
        #Left mask (fliplr)
        org_img_mask = np.fliplr(img_and_mask[1][:frame_height, :pic_width])
        img_mask_true_paralax = ~np.all(org_img_mask == black, axis=-1)
        img_mask_resized = np.array(cv2.resize(img_mask_true_paralax.astype(np.uint8)*255, (new_width, new_height)) > 0).astype(np.uint8)*255
        left_mask_input.append(img_mask_resized)
        
        #Left image (fliplr)
        org_img = np.fliplr(img_and_mask[0][:frame_height, :pic_width])
        img_resized = cv2.resize(org_img, (new_width, new_height))
        left_input.append(img_resized)
        
    right_mask_input = np.array(right_mask_input)
    left_mask_input = np.array(left_mask_input)
    
    right_input = np.array(right_input)
    left_input = np.array(left_input)

    #TODO: Investigate why the masks almost dont do anything at all, i can invert the mask and get almost the same result
    print("generating left side images")
    left_frames = generate_infilled_frames(left_input, left_mask_input)
    print("generating right side images")
    right_frames = generate_infilled_frames(right_input, right_mask_input)

    sttart = 0
    if not keep_first_three:
        sttart = 3

    eend = len(left_frames)
    if not keep_last_three:
        eend -= 3

    proccessed_frames = []
    for j in range(sttart, eend):
        left_img = cv2.resize(np.fliplr(left_frames[j]), (pic_width, frame_height))
        right_img = cv2.resize(right_frames[j], (pic_width, frame_height))


        right_org_img = chunk[j][0][:frame_height, pic_width:].copy()
        left_org_img = chunk[j][0][:frame_height, :pic_width].copy()
        right_mask = chunk[j][1][:frame_height, pic_width:]
        left_mask = chunk[j][1][:frame_height, :pic_width]

        #we invert the mask here, originaly black is source material ie mask = True, white is area that needs infill ie mask = False
        right_black_mask = np.all(right_mask == black, axis=-1)
        left_black_mask = np.all(left_mask == black, axis=-1)

        #We update the org image so it contains the rigthpixels
        left_org_img[~left_black_mask] = left_img[~left_black_mask]
        right_org_img[~right_black_mask] = right_img[~right_black_mask]

        #We save this basic image witout blending for use as input to next batch
        basic_out_image = cv2.hconcat([left_org_img, right_org_img])
        basic_out_image_uint8 = np.clip(basic_out_image, 0, 255).astype(np.uint8)
        proccessed_frames.append(basic_out_image_uint8)

        # Apply edge blending
        # if we dont we get a uggly halo effect around forground objects
        # This no longer works now that the masks are based on normals....
        right_mask_blue = mark_lower_side(right_mask)
        right_backedge_mask = np.all(right_mask_blue == blue, axis=-1)
        left_mask_blue = mark_lower_side(left_mask)
        left_backedge_mask = np.all(left_mask_blue == blue, axis=-1)

        right_backedge_mask = binary_dilation(right_backedge_mask, iterations = 6)
        left_backedge_mask = binary_dilation(left_backedge_mask, iterations = 6)

        right_mask_float = right_backedge_mask.astype(np.float32)
        left_mask_float = left_backedge_mask.astype(np.float32)


        # Choose a kernel size and sigma for the Gaussian blur (tweak as needed).
        kernel_size = (15, 15)
        sigma = 0  # let OpenCV choose based on kernel size


        # Apply Gaussian blur to get soft alpha masks.
        right_alpha = cv2.GaussianBlur(right_mask_float, kernel_size, sigma)
        left_alpha = cv2.GaussianBlur(left_mask_float, kernel_size, sigma)

        # Expand dimensions to match image shape (H, W, 1).
        right_alpha = right_alpha[..., np.newaxis]
        left_alpha = left_alpha[..., np.newaxis]

        # Now blend: use the soft alpha to mix the original image with the existing one.
        # When alpha is 1, original image takes full weight; when 0, the destination image is preserved.
        left_img = left_alpha * left_img + (1 - left_alpha) * left_org_img
        right_img = right_alpha * right_img + (1 - right_alpha) * right_org_img

        # Finally, concatenate the blended images.
        out_image = cv2.hconcat([left_img, right_img])

        out_image_uint8 = np.clip(out_image, 0, 255).astype(np.uint8)
        out.write(cv2.cvtColor(out_image_uint8, cv2.COLOR_RGB2BGR))

    return proccessed_frames

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Video Crafter infill script')
    parser.add_argument('--sbs_color_video', type=str, required=True, help='side by side stereo video renderd with point clouds in the masked area')
    parser.add_argument('--sbs_mask_video', type=str, required=True, help='side by side stereo video mask')
    parser.add_argument('--max_frames', default=-1, type=int, help='quit after max_frames nr of frames', required=False)
    parser.add_argument('--num_inference_steps', default=3, type=int, help='Numer of defussion steps. More look better but is slower', required=False)
    
    
    args = parser.parse_args()
    
    num_inference_steps = args.num_inference_steps

    DEVICE = 'cuda' if torch.cuda.is_available() else 'cpu'

    frames_chunk=25


    if not os.path.isfile(args.sbs_color_video):
        raise Exception("input sbs_color_video does not exist")

    if not os.path.isfile(args.sbs_mask_video):
        raise Exception("input sbs_mask_video does not exist")

    raw_video = cv2.VideoCapture(args.sbs_color_video)
    frame_width, frame_height = int(raw_video.get(cv2.CAP_PROP_FRAME_WIDTH)), int(raw_video.get(cv2.CAP_PROP_FRAME_HEIGHT))
    frame_rate = raw_video.get(cv2.CAP_PROP_FPS)
    out_size = (frame_width, frame_height)

    mask_video = cv2.VideoCapture(args.sbs_mask_video)

    output_video_file = args.sbs_color_video+"_infilled.mkv"

    codec = cv2.VideoWriter_fourcc(*"FFV1")
    out = cv2.VideoWriter(output_video_file, codec, frame_rate, (frame_width, frame_height))

    img2vid_path = 'weights/stable-video-diffusion-img2vid-xt-1-1'
    unet_path = 'StereoCrafter/weights/StereoCrafter'

    image_encoder = CLIPVisionModelWithProjection.from_pretrained(
        img2vid_path,
        subfolder="image_encoder",
        variant="fp16",
        torch_dtype=torch.float16
    )

    vae = AutoencoderKLTemporalDecoder.from_pretrained(
        img2vid_path,
        subfolder="vae",
        variant="fp16",
        torch_dtype=torch.float16
    )

    unet = UNetSpatioTemporalConditionModel.from_pretrained(
        unet_path,
        subfolder="unet_diffusers",
        low_cpu_mem_usage=True,
        # variant="fp16",
        torch_dtype=torch.float16
    )

    image_encoder.requires_grad_(False)
    vae.requires_grad_(False)
    unet.requires_grad_(False)

    pipeline = StableVideoDiffusionInpaintingPipeline.from_pretrained(
        img2vid_path,
        image_encoder=image_encoder,
        vae=vae,
        unet=unet,
        torch_dtype=torch.float16,
    )
    pipeline = pipeline.to("cuda")

    frame_buffer = []
    first_chunk = True
    last_chunk = False
    frame_n = 0
    while raw_video.isOpened():
        print(f"Frame: {frame_n} {frame_n/frame_rate}s")
        frame_n += 1
        ret, raw_frame = raw_video.read()
        if not ret:
            break

        rgb = cv2.cvtColor(raw_frame, cv2.COLOR_BGR2RGB)

        ret, mask_frame = mask_video.read()
        mask_frame = cv2.cvtColor(mask_frame, cv2.COLOR_BGR2RGB)

        #bg_color_infill_detect = np.array([0, 255, 0], dtype=np.uint8)
        #bg_mask = np.all(rgb == bg_color_infill_detect, axis=-1)
        #img_mask = np.zeros((frame_height, frame_width), dtype=np.uint8)
        #rgb[bg_mask] = black

        frame_buffer.append([rgb, mask_frame])

        if len(frame_buffer) >= frames_chunk:
            proccessed_frames = deal_with_frame_chunk(first_chunk, frame_buffer, out, last_chunk)

            # the first 3 frames are not used (unless this is the first chunk), and the last 3 frames are not used
            if first_chunk:
                #keep overlap
                first_chunk = False
            frame_buffer = [
                # have tried priming with previously generated frames: (proccessed_frames[-5], frame_buffer[-5][1])
                # It does not genrerate great results

                (proccessed_frames[-6], frame_buffer[-6][1]),# we prime the next round with some frames
                (proccessed_frames[-5], frame_buffer[-5][1]),
                (proccessed_frames[-4], frame_buffer[-4][1]),
                frame_buffer[-3],# the last 3 frames tend to be pretty bad so we dont prime with them
                frame_buffer[-2],
                frame_buffer[-1],
            ]#reset but keep overlapp

        if frame_n == args.max_frames:
            break

    last_chunk = True
    #Append final three frames or whatever is left
    deal_with_frame_chunk(first_chunk, frame_buffer, out, last_chunk)

    raw_video.release()
    mask_video.release()
    out.release()