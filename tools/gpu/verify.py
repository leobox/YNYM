"""Run real Arc GPU operations; fail explicitly instead of silently using CPU."""
import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


def pytorch_check():
    import torch

    info = {"version": torch.__version__, "available": torch.xpu.is_available()}
    if not info["available"]:
        raise RuntimeError(f"XPU unavailable: {info}; check Intel graphics driver")
    devices = [torch.xpu.get_device_name(i) for i in range(torch.xpu.device_count())]
    info["devices"] = devices
    matches = [i for i, name in enumerate(devices) if "A350M" in name]
    if not matches:
        raise RuntimeError(f"A350M not found: {devices}")
    device = torch.device(f"xpu:{matches[0]}")
    info["selected_device"] = str(device)
    info["memory_bytes"] = torch.xpu.get_device_properties(device).total_memory
    torch.manual_seed(42)
    a = torch.randn(512, 512, dtype=torch.float32)
    b = torch.randn(512, 512, dtype=torch.float32)
    reference = a @ b
    ag, bg = a.to(device), b.to(device)
    result = ag @ bg
    torch.xpu.synchronize(device)
    torch.testing.assert_close(result.cpu(), reference, rtol=1e-3, atol=1e-3)
    info["max_abs_error"] = float((result.cpu() - reference).abs().max())
    # Include backward propagation, not just device enumeration.
    x = torch.tensor([1., 2., 3.], device=device, requires_grad=True)
    x.square().sum().backward()
    torch.testing.assert_close(x.grad.cpu(), torch.tensor([2., 4., 6.]))
    info["backward"] = "passed"
    for _ in range(3):
        result = ag @ bg
    torch.xpu.synchronize(device)
    start = time.perf_counter()
    for _ in range(20):
        result = ag @ bg
    torch.xpu.synchronize(device)
    info["resident_gpu_matmul_ms"] = (time.perf_counter() - start) * 1000 / 20
    info["status"] = "passed"
    return info


def openvino_check():
    import numpy as np
    import openvino as ov
    from openvino import opset13 as ops

    core = ov.Core()
    devices = {d: core.get_property(d, "FULL_DEVICE_NAME") for d in core.available_devices}
    matches = [d for d, name in devices.items() if d.startswith("GPU") and "A350M" in name]
    if not matches:
        raise RuntimeError(f"A350M not found: {devices}")
    device = matches[0]
    parameter = ops.parameter([1, 4], np.float32, name="input")
    model = ov.Model([ops.relu(ops.multiply(parameter, ops.constant(np.float32(2))))], [parameter])
    compiled = core.compile_model(model, device)
    data = np.array([[-2, -1, 1, 2]], dtype=np.float32)
    result = compiled([data])[0]
    np.testing.assert_allclose(result, np.maximum(data * 2, 0), rtol=1e-5, atol=1e-5)
    return {"status": "passed", "version": ov.__version__, "devices": devices,
            "selected_device": device, "execution_devices": compiled.get_property("EXECUTION_DEVICES"),
            "output": result.tolist()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=["pytorch", "openvino"])
    parser.add_argument("--output", type=Path, default=Path(__file__).parent / "results" / "latest.json")
    args = parser.parse_args()
    if args.backend:
        try:
            result = {"pytorch": pytorch_check, "openvino": openvino_check}[args.backend]()
        except Exception as exc:
            result = {"status": "failed", "error": str(exc)}
        print(json.dumps(result, ensure_ascii=True))
        return 0 if result["status"] == "passed" else 1
    report = {"time_utc": datetime.now(timezone.utc).isoformat(), "python": sys.version,
              "executable": sys.executable, "backends": {}}
    args.output = args.output.resolve()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for backend in ("pytorch", "openvino"):
        try:
            process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--backend", backend],
                                     capture_output=True, text=True, timeout=180, cwd=args.output.parent)
            try:
                result = json.loads(process.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                result = {"status": "failed", "stdout": process.stdout}
            result["exit_code"] = process.returncode
            if process.returncode:
                result["status"] = "failed"
            if process.stderr:
                result["stderr"] = process.stderr
        except subprocess.TimeoutExpired:
            result = {"status": "failed", "error": "GPU test timed out after 180 seconds"}
        report["backends"][backend] = result
        print(f"{backend}: {result['status']}", flush=True)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    print(f"Report: {args.output}")
    return 0 if all(r["status"] == "passed" for r in report["backends"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
