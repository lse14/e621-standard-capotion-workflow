# OCR Model Download

`Install-WebUI.bat` installs the WebUI and OCR runtimes, but it does not download
or redistribute OCR model weights. OCR is disabled by default. The WebUI can be
used without these files; only a job where OCR is explicitly enabled is blocked
until the local OCR resource passes verification. `Install-WebUI.bat` has no
`-OcrMode` parameter.

Download the following three files from their official Paddle HTTPS URLs. Do not
rename, unpack, or edit them. Create `ocr-model-archives` in the project root and
place all three files directly in that directory:

| File | Official URL | Size (bytes) | SHA-256 |
| --- | --- | ---: | --- |
| `PP-OCRv5_server_det_infer.tar` | <https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv5_server_det_infer.tar> | 88340480 | `22a33e0ba6a21425ea4192da03bf4395c9a0c67902bd924b7328fc859073045d` |
| `PP-OCRv5_server_rec_infer.tar` | <https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-OCRv5_server_rec_infer.tar> | 84869120 | `d99be2ffd348943ab52876179168be4fb5b14f5f0812f2ae4c76d89ec2ea750a` |
| `PP-LCNet_x1_0_textline_ori_infer.tar` | <https://paddle-model-ecology.bj.bcebos.com/paddlex/official_inference_model/paddle3.0.0/PP-LCNet_x1_0_textline_ori_infer.tar> | 6871040 | `6171f69605215a85624d650e9079fa45f7c3eaf944296181bcc5395bf3ddc7f6` |

After the files are present, double-click `Install-WebUI.bat` again. The installer
verifies every archive, safely stages the resource, runs an offline CPU OCR probe
against the already installed project-local runtime, and publishes the OCR resource
only after that probe succeeds. It does not rebuild the OCR runtime or download
model files. Incomplete, renamed, corrupt, or hash-mismatched archives do not
publish a partial OCR resource.

The same guide is shown when an OCR-enabled job finds a missing or hash-mismatched
resource. Replace the affected archive in `ocr-model-archives` and double-click
`Install-WebUI.bat`; jobs with OCR disabled remain available.

These models remain local-only. Their upstream terms and license status are not a
permission to mirror them in a project Release; see
`docs/THIRD_PARTY_NOTICES.md` and the Paddle model list:
<https://www.paddleocr.ai/latest/en/version3.x/model_list.html>.
