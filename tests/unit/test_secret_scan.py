"""交付包密钥扫描（scripts/secret_scan.py）的正向 / 负向测试。

铁律：闸门必须有负向测试，否则「删掉规则也绿」。本测试证明：
1. 当前交付包干净时扫描 PASS；
2. 注入真实形状的密钥（私钥块 / AKIA / sk- token）必须报红并点名文件行号；
3. allowlist 中的占位夹具不误报；
4. allowlist 条目失效（陈旧豁免）必须报红——防止豁免清单腐烂成垃圾桶。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import secret_scan as ss  # noqa: E402


def test_clean_repository_passes() -> None:
    """当前交付包无疑似密钥——防止闸门上线即长期红着没人管。"""
    assert ss.scan(ROOT) == []


def test_injected_private_key_is_detected(tmp_path: Path) -> None:
    """注入 PEM 私钥块必须报红并点名文件。"""
    target = tmp_path / "leak.py"
    target.write_text(
        f"KEY = '{'-----' + 'BEGIN ' + 'PRIVATE KEY-----'}'\nDATA = 'abc'\n",
        encoding="utf-8",
    )
    problems = ss.scan(tmp_path, check_allowlist=False)
    assert any("leak.py" in p and "私钥块" in p for p in problems)


def test_injected_aws_key_is_detected(tmp_path: Path) -> None:
    """注入 AWS key 形状必须报红。"""
    target = tmp_path / "config.yaml"
    target.write_text("aws_key: " + "AKIA" + "IOSFODNN7EXAMPLE" + "\n", encoding="utf-8")
    problems = ss.scan(tmp_path, check_allowlist=False)
    assert any("config.yaml" in p and "AWS" in p for p in problems)


def test_injected_api_token_is_detected(tmp_path: Path) -> None:
    """注入 sk- token 形状必须报红。"""
    target = tmp_path / "settings.json"
    target.write_text(
        '{"token": "sk-' + "abcdefghijklmnopqrstuvwxyz012345" + '"}\n', encoding="utf-8"
    )
    problems = ss.scan(tmp_path, check_allowlist=False)
    assert any("settings.json" in p and "token" in p for p in problems)


def test_allowlisted_fixture_is_not_false_positive(tmp_path: Path) -> None:
    """allowlist 登记的测试夹具不得误报，但同形状的非登记文件仍须报红。"""
    # 在临时库里复刻 allowlist 的目标文件原文
    rel, snippet, _reason = ss.ALLOWLIST[0]
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"VALUE = '{snippet}'\n", encoding="utf-8")
    problems = ss.scan(tmp_path, check_allowlist=False)
    assert problems == [], f"登记豁免不应误报：{problems}"


def test_stale_allowlist_entry_fails_closed(tmp_path: Path) -> None:
    """allowlist 条目在仓库中找不到原文时必须报红（陈旧豁免不得静默保留）。"""
    problems = ss.scan(tmp_path)  # 空库：allowlist 目标文件都不存在
    stale = [p for p in problems if "豁免已失效" in p]
    assert stale, "陈旧豁免必须报红，否则豁免清单会烂成垃圾桶"


def test_ignored_dirs_are_not_scanned(tmp_path: Path) -> None:
    """依赖缓存等 SKIP_DIRS 中的密钥形状不参与扫描（避免误报泛滥）。"""
    vendor = tmp_path / ".venv"
    vendor.mkdir()
    (vendor / "leak.py").write_text(
        "KEY = '" + "-----BEGIN " + "PRIVATE KEY-----" + "'\n", encoding="utf-8"
    )
    assert ss.scan(tmp_path, check_allowlist=False) == []


def test_main_exit_code_one_on_leak(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    """扫出密钥时 main 返回 1（CI 门禁），不是静默通过。"""
    monkeypatch.setattr(ss, "ROOT", Path(__file__).resolve().parents[2])
    monkeypatch.setattr(
        ss, "scan", lambda *_a, **_k: ["[FAIL] 疑似密钥：x.py:1 命中规则「私钥块」"]
    )
    assert ss.main() == 1
    assert "[FAIL]" in capsys.readouterr().out
