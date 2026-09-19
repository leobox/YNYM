# Intel Arc 로컬 연산 환경

Windows 11 / Python 3.11용 선택적 개발환경. 프로젝트 공용 의존성은 변경하지 않는다.
실행 파일: `D:\leobox\tools\gpu\.venv\Scripts\python.exe`

## 사용

워크스페이스 루트에서:

```powershell
# GPU 진단: PyTorch와 OpenVINO 모두 실제 A350M 연산 성공 시에만 종료 코드 0
pwsh -NoProfile -File tools/gpu/run.ps1

# 이 환경으로 연구 스크립트 실행
pwsh -NoProfile -File tools/gpu/run.ps1 path/to/script.py

# 환경 복구 / 동일 버전 설치 / 노트북 커널 등록 / 진단
pwsh -NoProfile -File tools/gpu/setup.ps1
```

VS Code/Jupyter의 커널 선택에서 **Leobox Intel Arc (Python 3.11)**을 선택한다.
`setup.ps1`은 `uv`와 Python 3.11을 사용하며, 이 폴더의 전용 가상환경만 동기화한다.
PyPI와 PyTorch 공식 XPU 저장소에서 `requirements-lock.txt`의 정확한 버전을 설치한다.
이 lock은 Windows/Python 3.11 전용이며 Colab/Linux 환경에 그대로 적용하지 않는다.

## 포함 도구

- PyTorch XPU 2.14.0+xpu: GPU 텐서 연산 및 학습, Intel 런타임 포함
- OpenVINO 2026.4.0: Intel GPU 모델 추론
- NumPy, pandas, matplotlib, FinanceDataReader, requests
- ipykernel: 로컬 노트북 커널

기존 NumPy/pandas 코드는 이 커널로 실행해도 자동으로 GPU 연산이 되지 않는다.
연구 코드에 적용하려면 병목 측정과 CPU/GPU 결과 비교 후 별도 작업으로 전환한다.
실제 주문 API는 포함하지 않는다. 로컬 GPU는 원격 Colab이나 GitHub Actions에 연결되지 않는다.

## GPU 선택

Iris Xe와 Arc가 함께 있는 PC이므로 `GPU.0`이나 `xpu:0`을 Arc라고 가정하지 않는다.
`verify.py`는 장치 이름에서 A350M을 찾아 명시적으로 선택한다.

```python
import torch

devices = {i: torch.xpu.get_device_name(i) for i in range(torch.xpu.device_count())}
index = next(i for i, name in devices.items() if "A350M" in name)
device = torch.device(f"xpu:{index}")
x = torch.arange(8, dtype=torch.float32, device=device)
print(x.square().cpu())
```

## 검증과 제한

`results/latest.json`에 Python 경로, 백엔드 버전, 선택 장치, 실제 연산 결과를 기록한다.
PyTorch는 FP32 행렬곱의 CPU 결과 비교와 역전파를 검증한다.
측정 시간은 GPU에 이미 올려둔 512×512 행렬곱만 포함하며 프로젝트 전체 속도 향상을 의미하지 않는다.
OpenVINO는 A350M에 작은 모델을 명시적으로 컴파일하고 추론 결과를 비교한다.
한 라이브러리의 드라이버 충돌이 다른 진단까지 종료시키지 않도록 별도 프로세스로 실행한다.
CPU로 조용히 대체하지 않으며, 실패나 시간 초과 시 종료 코드 1을 반환한다.

FP64, 대형 모델의 메모리 한계, 모든 모델 연산, `torch.compile`은 이 검증 범위 밖이다.
`torch.compile`에는 추가 Level Zero SDK/컴파일 도구 준비가 필요할 수 있다.
`downloads/`, `.venv/`, `results/`는 Git에 포함하지 않는다.

## 드라이버

확인된 초기 드라이버: Arc `31.0.101.4255`, Iris Xe `31.0.101.4502`.
Intel 공식 `32.0.101.8993` 설치 파일은 `downloads/gfx_win_101.8993.exe`에 저장했다.
SHA256: `15A4E6127F775029A88E68A3E92C318938931FDEBB7A11022599511D5EC3CBCF`.
실행 전 공개 해시 일치와 Intel Corporation의 유효한 Authenticode 서명을 확인했다.
설치 로그: `downloads/driver-install.log`.
추가 소프트웨어 및 자동 재부팅 없이 `--silent --noExtras --report <log>`로 실행했다.
드라이버 설치는 `setup.ps1`에 포함하지 않는다.

공식 자료:

- [PyTorch Intel GPU 안내](https://github.com/pytorch/pytorch/blob/main/docs/source/notes/get_start_xpu.md)
- [Intel PyTorch 2.14 요구사항](https://www.intel.com/content/www/us/en/developer/articles/tool/pytorch-prerequisites-for-intel-gpu/2-14.html)
- [Intel Arc 드라이버](https://www.intel.com/content/www/us/en/download/785597/intel-arc-graphics-windows.html)
