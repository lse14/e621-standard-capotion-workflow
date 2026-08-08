from __future__ import annotations

import json
from io import BytesIO
from math import ceil
from pathlib import Path
from collections.abc import Sequence

import numpy as np
import open_clip
import torch
import torch.nn.functional as functional
from PIL import Image, ImageCms, ImageOps
from safetensors import safe_open
from safetensors.torch import load_file
from torch import Tensor, nn


JTP3_ARCHITECTURE = "naflexvit_so400m_patch16_siglip+rr_hydra"
JTP3_CLASS_COUNT = 7_504
JTP3_PATCH_SIZE = 16
JTP3_PATCH_DIM = JTP3_PATCH_SIZE * JTP3_PATCH_SIZE * 3


def _image_size_for_sequence(height: int, width: int, max_sequence: int = 1_024) -> tuple[int, int]:
    if height <= 0 or width <= 0:
        raise ValueError("image dimensions must be positive")
    max_rows = max(height // JTP3_PATCH_SIZE, 1)
    max_columns = max(width // JTP3_PATCH_SIZE, 1)
    if max_rows * max_columns <= max_sequence:
        return max_rows * JTP3_PATCH_SIZE, max_columns * JTP3_PATCH_SIZE

    def grid(ratio: float) -> tuple[int, int]:
        return (
            min(int(ceil((height * ratio) / JTP3_PATCH_SIZE)), max_rows),
            min(int(ceil((width * ratio) / JTP3_PATCH_SIZE)), max_columns),
        )

    lower = 1e-5
    upper = 1.0
    rows, columns = grid(lower)
    if rows * columns > max_sequence:
        raise ValueError("image aspect ratio cannot fit the JTP-3 sequence limit")
    while upper - lower >= 1e-5:
        midpoint = (lower + upper) / 2.0
        candidate_rows, candidate_columns = grid(midpoint)
        if candidate_rows * candidate_columns > max_sequence:
            upper = midpoint
        else:
            lower = midpoint
            rows, columns = candidate_rows, candidate_columns
            if rows * columns == max_sequence:
                break
    return rows * JTP3_PATCH_SIZE, columns * JTP3_PATCH_SIZE


def _prepare_jtp3_image(source: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(source)
    image.load()
    profile = image.info.get("icc_profile")
    if profile:
        try:
            image = ImageCms.profileToProfile(
                image,
                ImageCms.ImageCmsProfile(BytesIO(profile)),
                ImageCms.createProfile("sRGB"),
                outputMode="RGBA" if image.has_transparency_data else "RGB",
                renderingIntent=ImageCms.Intent.RELATIVE_COLORIMETRIC,
            )
        except (OSError, TypeError, ValueError):
            pass
    if image.has_transparency_data:
        rgba = image.convert("RGBA")
        image = Image.alpha_composite(Image.new("RGBA", rgba.size, (0, 0, 0, 255)), rgba).convert("RGB")
    else:
        image = image.convert("RGB")
    target_height, target_width = _image_size_for_sequence(image.height, image.width)
    if image.size != (target_width, target_height):
        image = image.resize((target_width, target_height), Image.Resampling.LANCZOS, reducing_gap=3.0)
    return image


def _patchify(images: Sequence[Image.Image]) -> tuple[Tensor, Tensor, Tensor]:
    patches = torch.zeros((len(images), 1_024, JTP3_PATCH_DIM), dtype=torch.float32)
    coordinates = torch.zeros((len(images), 1_024, 2), dtype=torch.int32)
    valid = torch.zeros((len(images), 1_024), dtype=torch.bool)
    for index, source in enumerate(images):
        image = _prepare_jtp3_image(source)
        pixels = np.asarray(image, dtype=np.uint8).copy()
        rows = image.height // JTP3_PATCH_SIZE
        columns = image.width // JTP3_PATCH_SIZE
        count = rows * columns
        values = (
            pixels.reshape(rows, JTP3_PATCH_SIZE, columns, JTP3_PATCH_SIZE, 3)
            .transpose(0, 2, 1, 3, 4)
            .reshape(count, JTP3_PATCH_DIM)
        )
        row_grid, column_grid = np.meshgrid(
            np.arange(rows, dtype=np.int32), np.arange(columns, dtype=np.int32), indexing="ij"
        )
        coords = np.stack((row_grid, column_grid), axis=-1).reshape(count, 2)
        patches[index, :count].copy_(torch.from_numpy(values).float())
        coordinates[index, :count].copy_(torch.from_numpy(coords))
        valid[index, :count] = True
    patches.div_(127.5).sub_(1.0)
    return patches, coordinates, valid


class _NaFlexEmbeddings(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.pos_embed = nn.Parameter(torch.empty(1, 16, 16, 1_152))
        self.proj = nn.Linear(768, 1_152)

    def forward(self, patches: Tensor, coordinates: Tensor, valid: Tensor) -> Tensor:
        patches = self.proj(patches)
        positional = self.pos_embed.permute(0, 3, 1, 2)
        for index in range(patches.shape[0]):
            count = int(valid[index].sum().item())
            grid = coordinates[index, :count].amax(dim=0) + 1
            rows, columns = int(grid[0].item()), int(grid[1].item())
            if (rows, columns) == (16, 16):
                embedding = positional.permute(0, 2, 3, 1).reshape(256, 1_152)
            else:
                embedding = functional.interpolate(
                    positional, size=(rows, columns), mode="bilinear", align_corners=False, antialias=True
                ).permute(0, 2, 3, 1).reshape(rows * columns, 1_152)
            patches[index, :count] = patches[index, :count] + embedding
        return patches


class _NaFlexAttention(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.qkv = nn.Linear(1_152, 3_456)
        self.proj = nn.Linear(1_152, 1_152)

    def forward(self, hidden: Tensor, mask: Tensor) -> Tensor:
        batch, sequence, _ = hidden.shape
        qkv = self.qkv(hidden).reshape(batch, sequence, 3, 16, 72)
        query, key, value = qkv.permute(2, 0, 3, 1, 4).unbind(0)
        attended = functional.scaled_dot_product_attention(query, key, value, attn_mask=mask)
        return self.proj(attended.transpose(1, 2).reshape(batch, sequence, 1_152))


class _NaFlexMlp(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.fc1 = nn.Linear(1_152, 4_304)
        self.fc2 = nn.Linear(4_304, 1_152)

    def forward(self, hidden: Tensor) -> Tensor:
        return self.fc2(functional.gelu(self.fc1(hidden), approximate="tanh"))


class _NaFlexBlock(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.attn = _NaFlexAttention()
        self.mlp = _NaFlexMlp()
        self.norm1 = nn.LayerNorm(1_152)
        self.norm2 = nn.LayerNorm(1_152)

    def forward(self, hidden: Tensor, mask: Tensor) -> Tensor:
        hidden = hidden + self.attn(self.norm1(hidden), mask)
        return hidden + self.mlp(self.norm2(hidden))


class _ClassProjection(nn.Module):
    def __init__(self, classes: int, features: int) -> None:
        super().__init__()
        self.weight = nn.Parameter(torch.empty(classes, features, 2))

    def forward(self, hidden: Tensor) -> Tensor:
        return torch.matmul(hidden.unsqueeze(-2), self.weight).squeeze(-2)


class _HydraPool(nn.Module):
    def __init__(self, classes: int) -> None:
        super().__init__()
        self.q = nn.Parameter(torch.empty(32, classes, 64))
        self.kv = nn.Linear(1_152, 4_096, bias=False)
        self.qk_norm = nn.RMSNorm(64, eps=1e-5, elementwise_affine=False)
        self.ff_norm = nn.LayerNorm(2_048)
        self.ff_in = nn.Linear(2_048, 12_288, bias=False)
        self.ff_out = nn.Linear(6_144, 2_048, bias=False)
        self.out_proj = _ClassProjection(classes, 2_048)

    def forward(self, hidden: Tensor, mask: Tensor) -> Tensor:
        batch, sequence, _ = hidden.shape
        query = self.q.expand(batch, -1, -1, -1)
        key_value = self.kv(hidden).reshape(batch, sequence, 2, 32, 64)
        key, value = key_value.permute(2, 0, 3, 1, 4).unbind(0)
        attended = functional.scaled_dot_product_attention(query, self.qk_norm(key), value, attn_mask=mask)
        attended = attended.transpose(1, 2).reshape(batch, -1, 2_048)
        normalized = self.ff_norm(attended)
        gate, values = self.ff_in(normalized).chunk(2, dim=-1)
        attended = attended + self.ff_out(functional.silu(gate) * values)
        gate, values = self.out_proj(attended).unbind(-1)
        return functional.silu(gate) * values


class _Jtp3Model(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.embeds = _NaFlexEmbeddings()
        self.blocks = nn.ModuleList(_NaFlexBlock() for _ in range(27))
        self.norm = nn.LayerNorm(1_152)
        self.attn_pool = _HydraPool(JTP3_CLASS_COUNT)

    def forward(self, patches: Tensor, coordinates: Tensor, valid: Tensor) -> Tensor:
        mask = valid[:, None, None, :]
        hidden = self.embeds(patches, coordinates, valid)
        for block in self.blocks:
            hidden = block(hidden, mask)
        return self.attn_pool(self.norm(hidden), mask)


class _WaifuV3Head(nn.Module):
    def __init__(self) -> None:
        super().__init__()
        self.layers = nn.Sequential(
            nn.Linear(768, 2_048), nn.ReLU(), nn.BatchNorm1d(2_048), nn.ReLU(),
            nn.Linear(2_048, 512), nn.ReLU(), nn.BatchNorm1d(512), nn.ReLU(),
            nn.Linear(512, 256), nn.ReLU(), nn.BatchNorm1d(256), nn.ReLU(),
            nn.Linear(256, 128), nn.ReLU(), nn.BatchNorm1d(128), nn.ReLU(),
            nn.Linear(128, 32), nn.ReLU(), nn.Linear(32, 1),
        )

    def forward(self, features: Tensor) -> Tensor:
        return self.layers(features)


class _FusionHead(nn.Module):
    def __init__(self, input_dim: int, hidden_dims: Sequence[int], dropout: float) -> None:
        super().__init__()
        layers: list[nn.Module] = []
        previous = input_dim
        for hidden in hidden_dims:
            layers.extend((nn.LayerNorm(previous), nn.Linear(previous, hidden), nn.GELU(), nn.Dropout(dropout)))
            previous = hidden
        self.trunk = nn.Sequential(*layers)
        self.reg_heads = nn.ModuleDict({name: nn.Linear(previous, 1) for name in ("aesthetic", "composition", "color", "sexual")})
        self.cls_head = nn.Linear(previous, 1)

    def forward(self, features: Tensor) -> Tensor:
        hidden = self.trunk(features)
        return torch.sigmoid(self.reg_heads["aesthetic"](hidden)).squeeze(-1) * 4.0 + 1.0


def _load_jtp3(path: Path, device: torch.device) -> _Jtp3Model:
    with safe_open(str(path), framework="pt", device="cpu") as tensors:
        metadata = dict(tensors.metadata() or {})
        if metadata.get("modelspec.architecture") != JTP3_ARCHITECTURE:
            raise RuntimeError("policy resource has an unsupported JTP-3 architecture")
        labels = str(metadata.get("classifier.labels", "")).splitlines()
        if len(labels) != JTP3_CLASS_COUNT:
            raise RuntimeError("policy JTP-3 label count is invalid")
        state = {key: tensors.get_tensor(key).float() for key in tensors.keys()}
    model = _Jtp3Model()
    model.load_state_dict(state, strict=True)
    return model.eval().requires_grad_(False).to(device=device, dtype=torch.float32)


def _load_waifu(path: Path, device: torch.device) -> _WaifuV3Head:
    model = _WaifuV3Head()
    model.load_state_dict(load_file(str(path), device="cpu"), strict=True)
    return model.eval().requires_grad_(False).to(device=device, dtype=torch.float32)


def _load_fusion(path: Path, device: torch.device) -> _FusionHead:
    with safe_open(str(path), framework="pt", device="cpu") as tensors:
        metadata = dict(tensors.metadata() or {})
        if metadata.get("format") != "fusion_multitask_v1" or int(metadata.get("input_dim", 0)) != 8_273:
            raise RuntimeError("policy fusion checkpoint metadata is invalid")
        config = json.loads(metadata.get("config_json", "{}"))
        models = config.get("models") if isinstance(config, dict) else None
        if not isinstance(models, dict) or models.get("include_waifu_score") is not True:
            raise RuntimeError("policy fusion checkpoint does not include the Waifu score")
        hidden = tuple(int(value) for value in json.loads(metadata.get("hidden_dims_json", "[]")))
        state = {key: tensors.get_tensor(key).float() for key in tensors.keys()}
    model = _FusionHead(8_273, hidden, float(metadata.get("dropout", 0.0)))
    model.load_state_dict(state, strict=True)
    return model.eval().requires_grad_(False).to(device=device, dtype=torch.float32)


class Lse14Scorer:
    def __init__(self, files: dict[str, Path], device: str) -> None:
        if device == "cuda" and not torch.cuda.is_available():
            raise RuntimeError("CUDA was requested for policy quality but is unavailable")
        self.device = torch.device("cuda" if device == "auto" and torch.cuda.is_available() else "cpu" if device == "auto" else device)
        self.jtp3 = _load_jtp3(files["jtp3/jtp-3-hydra.safetensors"], self.device)
        self.clip, _, self.clip_preprocess = open_clip.create_model_and_transforms(
            "ViT-L-14",
            pretrained=str(files["clip/ViT-L-14.pt"]),
            # The pinned OpenAI checkpoint is a TorchScript archive. The worker
            # verifies its SHA-256 before this trusted local load.
            weights_only=False,
        )
        self.clip = self.clip.eval().requires_grad_(False).to(self.device)
        self.waifu = _load_waifu(files["waifu/model.safetensors"], self.device)
        self.fusion = _load_fusion(files["fusion/5kdataset.safetensors"], self.device)
        self.load_count = 1
        self.device_name = str(self.device)

    @torch.inference_mode()
    def score(self, images: Sequence[Image.Image]) -> list[float]:
        if not images:
            return []
        patches, coordinates, valid = _patchify(images)
        jtp_features = self.jtp3(patches.to(self.device), coordinates.to(self.device), valid.to(self.device)).float()
        clip_input = torch.stack([self.clip_preprocess(image.convert("RGB")) for image in images]).to(self.device)
        clip_features = functional.normalize(self.clip.encode_image(clip_input).float(), dim=-1)
        waifu_score = self.waifu(clip_features).reshape(-1, 1)
        fused = torch.cat((jtp_features, clip_features, waifu_score), dim=-1)
        if fused.shape != (len(images), 8_273):
            raise RuntimeError(f"policy fusion feature shape is invalid: {tuple(fused.shape)}")
        return [float(value) for value in self.fusion(fused).detach().cpu().tolist()]
