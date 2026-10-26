#!/usr/bin/env python3
"""在 GitHub Actions 里持续尝试创建 OCI 免费 ARM 实例。

所有凭证都从环境变量读取（由仓库 Secrets 注入），不落盘到仓库里。
循环到「抢到 / 已存在 / 时间预算用完 / 配置错误」为止。

环境变量：
  OCI_USER, OCI_FINGERPRINT, OCI_TENANCY, OCI_REGION   —— 必填（Secrets）
  OCI_PRIVATE_KEY                                      —— 必填，API 私钥 PEM 全文（Secret）
  OCI_COMPARTMENT_ID, OCI_SUBNET_ID                    —— 必填（Secrets）
  OCI_SSH_PUBLIC_KEY                                   —— 必填，注入实例的 SSH 公钥（Secret）
  OCI_IMAGE_ID                                         —— 可选，留空自动选最新 ARM 镜像
  GRAB_NAME, GRAB_OCPUS, GRAB_MEM_GB, GRAB_BOOT_GB     —— 可选，默认 free-arm-1 / 1 / 6 / 100
  GRAB_BUDGET_SECONDS, GRAB_INTERVAL                   —— 可选，默认 530 秒 / 45 秒
"""
from __future__ import annotations

import os
import pathlib
import sys
import time
from datetime import datetime, timezone

import oci


def env(key: str, default: str = "") -> str:
    v = os.environ.get(key)
    return v if v not in (None, "") else default


USER = env("OCI_USER")
FINGERPRINT = env("OCI_FINGERPRINT")
TENANCY = env("OCI_TENANCY")
REGION = env("OCI_REGION", "ap-tokyo-1")
PRIVATE_KEY = env("OCI_PRIVATE_KEY")
COMPARTMENT = env("OCI_COMPARTMENT_ID") or TENANCY
SUBNET = env("OCI_SUBNET_ID")
SSH_PUB = env("OCI_SSH_PUBLIC_KEY")
IMAGE_ID = env("OCI_IMAGE_ID")

NAME = env("GRAB_NAME", "free-arm-1")
OCPUS = int(env("GRAB_OCPUS", "1"))
MEM_GB = int(env("GRAB_MEM_GB", "6"))
BOOT_GB = int(env("GRAB_BOOT_GB", "100"))
BUDGET = int(env("GRAB_BUDGET_SECONDS", "530"))
INTERVAL = int(env("GRAB_INTERVAL", "45"))

SHAPE = "VM.Standard.A1.Flex"
RETRYABLE_CODES = {"InternalError", "LimitExceeded", "TooManyRequests", "ServiceUnavailable"}


def log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {msg}", flush=True)


def fail(msg: str, code: int = 2) -> None:
    log(f"❌ {msg}")
    print(f"RESULT: ERROR {msg}")
    summary(f"❌ 抢机失败：{msg}")
    sys.exit(code)


def summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        pass


def write_config() -> str:
    """把 Secret 里的私钥写成文件，返回路径。"""
    d = pathlib.Path.home() / ".oci"
    d.mkdir(parents=True, exist_ok=True)
    key = d / "oci_api_key.pem"
    key.write_text(PRIVATE_KEY.strip() + "\n", encoding="utf-8")
    try:
        key.chmod(0o600)
    except Exception:
        pass
    return str(key)


def main() -> None:
    missing = [k for k, v in {
        "OCI_USER": USER, "OCI_FINGERPRINT": FINGERPRINT, "OCI_TENANCY": TENANCY,
        "OCI_PRIVATE_KEY": PRIVATE_KEY, "OCI_SUBNET_ID": SUBNET, "OCI_SSH_PUBLIC_KEY": SSH_PUB,
    }.items() if not v]
    if missing:
        fail(f"缺少环境变量/Secrets: {', '.join(missing)}", 2)

    key_file = write_config()
    config = {"user": USER, "fingerprint": FINGERPRINT, "tenancy": TENANCY,
              "region": REGION, "key_file": key_file}
    signer = oci.signer.Signer(tenancy=TENANCY, user=USER, fingerprint=FINGERPRINT,
                               private_key_file_location=key_file)
    compute = oci.core.ComputeClient(config, signer=signer)
    identity = oci.identity.IdentityClient(config, signer=signer)

    log(f"区域 {REGION} · 目标 {NAME} ({OCPUS}核/{MEM_GB}GB/{BOOT_GB}GB)")

    ads = [d.name for d in identity.list_availability_domains(COMPARTMENT).data]
    if not ads:
        fail("取不到可用域，检查凭证/区域")
    log(f"可用域: {', '.join(ads)}")

    def live_names() -> set:
        inst = compute.list_instances(COMPARTMENT).data
        return {i.display_name for i in inst
                if (i.lifecycle_state or "").upper() not in ("TERMINATED", "TERMINATING")}

    existing = live_names()
    if NAME in existing:
        log(f"实例 {NAME} 已存在，无需再抢")
        print(f"RESULT: ALREADY_EXISTS {NAME}")
        summary(f"✅ 实例 `{NAME}` 已经存在（可能是本机或上一次运行抢到的）。")
        return

    image_id = IMAGE_ID
    if not image_id:
        imgs = compute.list_images(
            COMPARTMENT, shape=SHAPE, operating_system="Canonical Ubuntu",
            sort_by="TIMECREATED", sort_order="DESC").data
        if not imgs:
            fail("找不到 Canonical Ubuntu 的 ARM 镜像")
        image_id = imgs[0].id
        log(f"镜像: {imgs[0].display_name}")

    deadline = time.time() + BUDGET
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        for ad in ads:
            if time.time() >= deadline:
                break
            log(f"第 {attempt} 轮 · 在 {ad} 尝试创建 …")
            details = oci.core.models.LaunchInstanceDetails(
                availability_domain=ad,
                compartment_id=COMPARTMENT,
                shape=SHAPE,
                shape_config=oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=OCPUS, memory_in_gbs=MEM_GB),
                source_details=oci.core.models.InstanceSourceViaImageDetails(
                    image_id=image_id, boot_volume_size_in_gbs=BOOT_GB),
                create_vnic_details=oci.core.models.CreateVnicDetails(
                    subnet_id=SUBNET, assign_public_ip=True),
                display_name=NAME,
                metadata={"ssh_authorized_keys": SSH_PUB.strip()},
            )
            try:
                resp = compute.launch_instance(details)
                inst = resp.data
                log(f"🎉 抢到了！instance={inst.id}")
                print(f"RESULT: CREATED {inst.id}")
                summary(f"🎉 **抢到了！** `{NAME}` @ {ad}\n\n- instance: `{inst.id}`\n- 区域: {REGION}")
                if resp.headers.get("opc-work-request-id"):
                    log(f"work request: {resp.headers['opc-work-request-id']}")
                return
            except oci.exceptions.ServiceError as e:
                code, msg = e.code or "", (e.message or "")
                if code == "TooManyRequests":
                    log("  → 被限流(429)，等 90s 再试")
                    time.sleep(90)
                elif "Out of host capacity" in msg or code in RETRYABLE_CODES:
                    if code == "LimitExceeded":
                        log(f"  ⚠️ {code}: {msg[:160]}（免费额度已满，停止）")
                        fail(f"配额不足：{msg[:160]}", 3)
                    log(f"  → {ad} 无容量（{code or 'capacity'}），稍后重试")
                elif code in ("NotAuthenticated", "NotAuthorizedOrNotFound"):
                    fail(f"{code}: {msg[:200]}（检查 Secrets 是否过期/填错）", 4)
                else:
                    log(f"  ⚠️ {code or '未知错误'}: {msg[:200]}")
                    fail(f"{code}: {msg[:200]}", 5)
            except Exception as e:  # noqa: BLE001
                log(f"  ⚠️ 调用异常: {str(e)[:200]}")
        if time.time() < deadline:
            log(f"本轮结束，{INTERVAL}s 后重试（剩余 {int(deadline - time.time())}s）")
            time.sleep(INTERVAL)

    log("本次运行时间预算用完，交给下一次 workflow 继续")
    print("RESULT: NO_CAPACITY")
    summary("⏳ 本轮没抢到（容量不足），已交给下一次运行继续。")
    sys.exit(0)


if __name__ == "__main__":
    main()
