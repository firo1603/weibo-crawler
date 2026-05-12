# 完整文件，已合入 Edge/msedgedriver 版本检查与自动更新逻辑
from selenium import webdriver
from selenium.webdriver.edge.service import Service
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.by import By
import json
import subprocess
import sys
from pathlib import Path
import tempfile
import urllib.request
import zipfile
import shutil
import time

try:
    import winreg  # type: ignore
except ImportError:  # pragma: no cover
    winreg = None


def query_registry(root, path, value_name):
    if winreg is None:
        return None
    try:
        with winreg.OpenKey(root, path) as key:
            value, _ = winreg.QueryValueEx(key, value_name)
            return str(value)
    except FileNotFoundError:
        return None
    except OSError:
        return None


def get_edge_version():
    candidates = [
        (winreg.HKEY_CURRENT_USER, r"Software\\Microsoft\\Edge\\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\\Microsoft\\Edge\\BLBeacon"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\\WOW6432Node\\Microsoft\\Edge\\BLBeacon"),
    ]
    for root, path in candidates:
        version = query_registry(root, path, "version")
        if version:
            return version
    return None


def get_driver_version(driver_path: Path):
    command = [str(driver_path), "--version"]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            encoding="utf-8",
            errors="ignore",
        )
    except FileNotFoundError:
        return None
    output = (completed.stdout or completed.stderr or "").strip()
    if not output:
        return None
    for token in output.split():
        if token and token[0].isdigit():
            return token
    return None


def compare_versions(edge_version, driver_version):
    if not edge_version or not driver_version:
        return False
    edge_major = edge_version.split(".")[0]
    driver_major = driver_version.split(".")[0]
    return edge_major == driver_major


def download_driver(version, target_path: Path):
    """
    从 Microsoft 存储下载与 Edge 版本对应的 edgedriver zip，
    解压并复制 msedgedriver.exe 到 target_path。
    返回 True 表示成功。
    """
    url = f"https://msedgedriver.microsoft.com/{version}/edgedriver_win64.zip"
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            zip_path = Path(tmpdir) / "edgedriver.zip"
            urllib.request.urlretrieve(url, zip_path)
            with zipfile.ZipFile(zip_path) as zf:
                for member in zf.namelist():
                    if member.endswith("msedgedriver.exe"):
                        zf.extract(member, tmpdir)
                        extracted = Path(tmpdir) / member
                        target_path.parent.mkdir(parents=True, exist_ok=True)
                        try:
                            # 直接复制解压出的可执行文件到目标位置（跨盘复制）
                            shutil.copy2(str(extracted), str(target_path))
                        except Exception:
                            # 尝试先复制到临时文件再替换（降低部分锁定情况下失败概率）
                            tmp_target = target_path.with_suffix(".tmp")
                            try:
                                shutil.copy2(str(extracted), str(tmp_target))
                                try:
                                    tmp_target.replace(target_path)
                                except Exception:
                                    # 最终降级为直接复制（可能仍失败）
                                    shutil.copy2(str(tmp_target), str(target_path))
                            except Exception:
                                return False
                        return True
    except Exception:
        return False
    return False


def ensure_driver(driver_path: Path):
    """
    检查本机 Edge 与驱动主版本号是否匹配；如果不匹配，尝试自动下载对应版本的驱动并替换。
    自动更新为默认开启；若需要禁用，请在调用前设置环境变量 NO_UPDATE=1（可按需改造为命令行参数）。
    返回最终的 driver_path（Path 对象），以及 True/False 表示驱动可用且版本匹配。
    """
    no_update = (os.environ.get("NO_UPDATE", "") == "1") if "os" in globals() or True else False
    edge_version = get_edge_version()
    driver_version = get_driver_version(driver_path)
    print(f"Detected Edge version   : {edge_version or 'unknown'}")
    print(f"Detected Driver version : {driver_version or 'unknown'}")
    print(f"Driver path             : {driver_path}")

    if compare_versions(edge_version, driver_version):
        print("Edge and driver major versions match.")
        return driver_path, True

    print("Edge and driver major versions do NOT match.")
    if no_update:
        print("NO_UPDATE set -> 不进行自动更新。")
        return driver_path, False

    if not edge_version:
        print("无法从注册表读取 Edge 版本，跳过自动下载。")
        return driver_path, False

    print("尝试下载匹配的 msedgedriver ...")
    success = download_driver(edge_version, driver_path)
    if not success:
        print("下载或安装驱动失败。")
        return driver_path, False

    # 再次确认版本
    driver_version = get_driver_version(driver_path)
    if compare_versions(edge_version, driver_version):
        print("驱动已更新且版本匹配。")
        return driver_path, True

    print("驱动更新后版本仍不匹配。")
    return driver_path, False


import os  # placed after ensure_driver reference for clarity


MAX_STORED_COOKIES = 5
COOKIE_FILE = Path("cookie.txt")
WANTED_COOKIE_ORDER = (
    "SUB",
    "SUBP",
    "SCF",
    "_T_WM",
    "ALF",
    "SSOLoginState",
    "WEIBOCN_FROM",
    "XSRF-TOKEN",
    "M_WEIBOCN_PARAMS",
    "WBPSESS",
)
WANTED_COOKIES = set(WANTED_COOKIE_ORDER)


def atomic_write_text(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f"{path.name}.",
        suffix=".tmp",
    )
    tmp_path = Path(tmp_name)
    try:
        os.close(fd)
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, path)
    except Exception:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        raise


def write_config_file(path: Path, config):
    atomic_write_text(path, json.dumps(config, ensure_ascii=False, indent=2))


def normalize_cookie_string(cookie_str):
    cookie_map = {}
    for chunk in cookie_str.split(";"):
        pair = chunk.strip()
        if not pair or "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        name = name.strip()
        if not name:
            continue
        cookie_map[name] = value.strip()

    ordered_names = [name for name in WANTED_COOKIE_ORDER if name in cookie_map]
    ordered_names.extend(sorted(name for name in cookie_map if name not in WANTED_COOKIES))
    return "; ".join(f"{name}={cookie_map[name]}" for name in ordered_names)


def read_cookie_file(path: Path):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        cookies = []
        for line in lines:
            normalized = normalize_cookie_string(line.strip())
            if normalized:
                cookies.append(normalized)
        return cookies
    except FileNotFoundError:
        return []
    except Exception:
        return []


def write_cookie_file(path: Path, cookies):
    trimmed = [normalize_cookie_string(cookie) for cookie in cookies[-MAX_STORED_COOKIES:]]
    atomic_write_text(path, "\n".join(cookie for cookie in trimmed if cookie))


def dedup_preserve_order(items):
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def apply_cookie_string(driver, cookie_str):
    added = 0
    for chunk in cookie_str.split(";"):
        pair = chunk.strip()
        if not pair or "=" not in pair:
            continue
        name, value = pair.split("=", 1)
        driver.add_cookie({"name": name.strip(), "value": value.strip(), "domain": ".weibo.cn"})
        added += 1
    if added:
        driver.refresh()
    return added > 0


def wait_login(driver, timeout=180):
    try:
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='logout'], a.navbar-item[href*='logout']"))
        )
        return True
    except Exception:
        return False


def collect_cookie_string(driver):
    time.sleep(2)
    cookies = driver.get_cookies()
    pairs = []
    for item in cookies:
        name = item.get("name")
        if name in WANTED_COOKIES:
            pairs.append(f"{name}={item.get('value', '')}")
    return normalize_cookie_string("; ".join(pairs))


def get_weibo_cookies():
    driver_filepath = Path("msedgedriver.exe").resolve()
    driver_path, ok = ensure_driver(driver_filepath)
    if not ok:
        print("驱动不可用或版本不匹配，停止运行。")
        sys.exit(1)

    service = Service(str(driver_path))
    options = webdriver.EdgeOptions()
    # 使用移动 UA，避免桌面站点跳转导致 Cookie 域不匹配
    options.add_argument(
        "--user-agent=Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    )

    driver = webdriver.Edge(service=service, options=options)
    desktop_url = "https://weibo.cn/"
    target_url = "https://m.weibo.cn/"
    login_url = "https://passport.weibo.com/sso/signin?entry=wapsso&source=wapsso&url=https%3A%2F%2Fm.weibo.cn%2F"

    def validate_cookie(cookie_str):
        try:
            driver.delete_all_cookies()
            driver.get(desktop_url)
            if not apply_cookie_string(driver, cookie_str):
                return False
            return wait_login(driver, timeout=15)
        except Exception:
            return False

    def collect_valid_cookie():
        candidate = collect_cookie_string(driver)
        if not candidate:
            return None
        if validate_cookie(candidate):
            return candidate
        print("新采集到的 cookie 校验失败，已丢弃。")
        return None

    try:
        config = {}
        config_path = Path("config.json")
        try:
            with config_path.open("r", encoding="utf-8") as f:
                config = json.load(f)
        except FileNotFoundError:
            config = {}

        stored_cookies = read_cookie_file(COOKIE_FILE)
        print(f"检测 cookie.txt（{len(stored_cookies)} 条）...")

        valid_cookies = []
        for idx, ck in enumerate(stored_cookies, start=1):
            if validate_cookie(ck):
                print(f"第 {idx} 条 cookie 有效，已保留。")
                valid_cookies.append(ck)
            else:
                print(f"第 {idx} 条 cookie 无效，已丢弃。")

        # 尝试基于最新有效 cookie 刷新一条新 cookie
        new_cookie = None
        if valid_cookies:
            latest_valid = valid_cookies[-1]
            driver.delete_all_cookies()
            driver.get(desktop_url)
            if validate_cookie(latest_valid):
                driver.get(target_url)
                new_cookie = collect_valid_cookie()
            else:
                print("最新有效 cookie 再次校验失败，进入登录流程。")

        # 没有拿到新 cookie 时，走手动登录获取新 cookie
        if not new_cookie:
            if valid_cookies:
                print("未能基于已有有效 cookie 刷新出新 cookie，请手动扫码/登录...")
            else:
                print("未找到可用 cookie，请手动扫码/登录...")
            driver.get(login_url)
            if wait_login(driver):
                driver.get(target_url)
                new_cookie = collect_valid_cookie()
                if not new_cookie:
                    print("登录成功，但未采集到新的 cookie。")
            elif valid_cookies:
                print("在指定时间内未检测到登录成功，将继续使用已有有效 cookie。")
            else:
                print("在指定时间内未检测到登录成功。")

        merged = list(valid_cookies)
        if new_cookie:
            merged.append(new_cookie)
        merged = dedup_preserve_order(merged)
        final_cookies = merged[-MAX_STORED_COOKIES:]

        # 若 config.json 中 "cookie" 已经是文件路径，则不覆盖，保持文件路径模式
        cookie_field = config.get("cookie", "")
        use_file_mode = isinstance(cookie_field, str) and cookie_field.endswith(".txt")

        if final_cookies:
            write_cookie_file(COOKIE_FILE, final_cookies)
            if not use_file_mode:
                selected_cookie = new_cookie or final_cookies[-1]
                config["cookie"] = selected_cookie
                write_config_file(config_path, config)
            print(
                f"Cookie 已更新：cookie.txt 保留 {len(final_cookies)} 条（最多 {MAX_STORED_COOKIES} 条）"
                + ("，最新一条已写入 config.json。" if not use_file_mode else "（config.json 保持文件路径模式）。")
            )
        else:
            write_cookie_file(COOKIE_FILE, [])
            if not use_file_mode:
                config["cookie"] = ""
                write_config_file(config_path, config)
            print("未能获取有效 cookie，cookie.txt 已清空" + ("，并已清空 config.json 中的 cookie。" if not use_file_mode else "。"))
    finally:
        driver.quit()


if __name__ == "__main__":
    get_weibo_cookies()
