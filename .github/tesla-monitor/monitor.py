#!/usr/bin/env python3
"""Watch Spain Tesla Model Y inventory through a public inventory index.

Tesla blocks GitHub-hosted runner IPs, so this monitor reads the public
Teslastats inventory fragment using exactly the Spain + Model Y filters exposed
by that site's UI. New vehicle identifiers create a GitHub issue assigned to
the repository owner; GitHub then delivers the issue notification by email.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from curl_cffi import requests

TESLA_URL = (
    "https://www.tesla.com/es_ES/inventory/new/my"
    "?arrangeby=plh&zip=46183&PaymentType=cash"
)
SOURCE_PAGE = "https://teslainventory.teslastats.no/es/"
SOURCE_API = "https://teslainventory.teslastats.no/api/get-cars.php"
DEFAULT_STATE_PATH = Path(__file__).with_name("state.json")
DEFAULT_ALERT_ASSIGNEE = "routerpipe-byte"


@dataclass(frozen=True)
class Vehicle:
    vehicle_id: str
    trim: str
    paint: str
    price: str
    mileage: str
    discount: str
    wheels: str
    interior: str
    seats: str
    autopilot: str
    requests: str
    location: str
    source_url: str


def log(message: str) -> None:
    print(f"[tesla-model-y-monitor] {message}", flush=True)


def normalize_text(value: str) -> str:
    return " ".join(value.replace("\xa0", " ").split())


def extract_csrf(html: str) -> str:
    patterns = (
        r"window\.__CSRF__\s*=\s*[\"']([^\"']+)",
        r"name=[\"']csrf[\"'][^>]*value=[\"']([^\"']+)",
        r"value=[\"']([^\"']+)[\"'][^>]*name=[\"']csrf[\"']",
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return match.group(1)
    raise RuntimeError("Teslastats page did not expose its CSRF token")


def expected_inventory_count(soup: BeautifulSoup) -> int | None:
    text = normalize_text(soup.get_text(" ", strip=True))
    match = re.search(
        r"Total\s+inventory\s+available:\s*([0-9][0-9,.\s]*)",
        text,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    digits = re.sub(r"\D", "", match.group(1))
    return int(digits) if digits else None


def canonical_header(value: str, index: int) -> str:
    key = normalize_text(value).lower()
    key = (
        key.replace("é", "e")
        .replace("è", "e")
        .replace("ó", "o")
        .replace("ö", "o")
    )
    if "model" in key:
        return "trim"
    if "paint" in key or "color" in key:
        return "paint"
    if "price" in key:
        return "price"
    if "mile" in key or "odometer" in key or "kilomet" in key:
        return "mileage"
    if "discount" in key:
        return "discount"
    if "wheel" in key:
        return "wheels"
    if "decor" in key or "interior" in key:
        return "interior"
    if "seat" in key:
        return "seats"
    if key == "ap" or "autopilot" in key:
        return "autopilot"
    if "location" in key or "ubic" in key:
        return "location"
    if "ⓡ" in key:
        return "requests"
    return f"column_{index}"


def fields_from_row(headers: list[str], cells: list[str]) -> dict[str, str]:
    """Map a result row to stable field names, with a positional fallback."""
    if headers and len(headers) == len(cells):
        mapped: dict[str, str] = {}
        for index, (header, cell) in enumerate(zip(headers, cells)):
            key = canonical_header(header, index)
            if key == "price" and "price" in mapped:
                key = "discount"
            elif key == "discount" and "discount" in mapped:
                key = f"column_{index}"
            mapped[key] = normalize_text(cell)
        if mapped.get("trim") and mapped.get("location"):
            return mapped

    values = [normalize_text(cell) for cell in cells]
    if values and not values[0]:
        values = values[1:]
    if len(values) < 11:
        raise RuntimeError(
            f"Unexpected Teslastats row layout: {len(cells)} cells: {cells!r}"
        )
    values = values[:11]
    keys = (
        "trim",
        "paint",
        "price",
        "mileage",
        "discount",
        "wheels",
        "interior",
        "seats",
        "autopilot",
        "requests",
        "location",
    )
    return dict(zip(keys, values))


def parse_inventory(html: str) -> tuple[list[Vehicle], dict[str, Any]]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table")
    if table is None:
        plain = normalize_text(soup.get_text(" ", strip=True))
        raise RuntimeError(f"Teslastats response contained no table: {plain[:400]!r}")

    header_cells: list[str] = []
    vehicles: list[Vehicle] = []

    for row in table.find_all("tr"):
        cells = [
            normalize_text(cell.get_text(" ", strip=True))
            for cell in row.find_all(["th", "td"], recursive=False)
        ]
        if not cells:
            continue

        vehicle_input = row.find(
            "input",
            attrs={
                "name": lambda value: isinstance(value, str)
                and value.lower().startswith("vmid")
            },
        )
        if vehicle_input is None:
            if not header_cells and any("model" in cell.lower() for cell in cells):
                header_cells = cells
            continue

        vehicle_id = normalize_text(str(vehicle_input.get("value") or ""))
        if not vehicle_id:
            raise RuntimeError("Teslastats vehicle row had an empty identifier")

        fields = fields_from_row(header_cells, cells)
        link = row.find("a", href=True)
        source_url = (
            urljoin(SOURCE_API, str(link["href"])) if link is not None else SOURCE_PAGE
        )

        vehicles.append(
            Vehicle(
                vehicle_id=vehicle_id,
                trim=fields.get("trim", ""),
                paint=fields.get("paint", ""),
                price=fields.get("price", ""),
                mileage=fields.get("mileage", ""),
                discount=fields.get("discount", ""),
                wheels=fields.get("wheels", ""),
                interior=fields.get("interior", ""),
                seats=fields.get("seats", ""),
                autopilot=fields.get("autopilot", ""),
                requests=fields.get("requests", ""),
                location=fields.get("location", ""),
                source_url=source_url,
            )
        )

    unique = {vehicle.vehicle_id: vehicle for vehicle in vehicles}
    vehicles = [unique[key] for key in sorted(unique)]

    expected = expected_inventory_count(soup)
    if expected is None:
        raise RuntimeError("Teslastats response did not contain a verifiable inventory count")
    if expected != len(vehicles):
        raise RuntimeError(
            f"Teslastats said {expected} vehicles but parser found {len(vehicles)}"
        )

    return vehicles, {
        "expected_count": expected,
        "source_page": SOURCE_PAGE,
        "source_api": SOURCE_API,
        "filters": {"land": "es", "model": "y"},
    }


def fetch_inventory() -> tuple[list[Vehicle], dict[str, Any]]:
    last_error: Exception | None = None

    for attempt in range(1, 4):
        try:
            session = requests.Session(impersonate="chrome")
            headers = {
                "Accept-Language": "es-ES,es;q=0.9,en;q=0.7",
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            }
            page = session.get(SOURCE_PAGE, headers=headers, timeout=30)
            if page.status_code != 200:
                raise RuntimeError(f"Teslastats page returned HTTP {page.status_code}")
            csrf = extract_csrf(page.text)

            params = [
                ("csrf", csrf),
                ("country", "US"),
                ("land", "es"),
                ("state", "ALL"),
                ("seats", "all"),
                ("model", "y"),
                ("twentyfour", "false"),
                ("discount", "false"),
                ("badge[]", "all"),
                ("paint[]", "all"),
                ("decor[]", "all"),
            ]
            response = session.get(
                SOURCE_API,
                params=params,
                headers={
                    **headers,
                    "Accept": "text/html, */*; q=0.01",
                    "Referer": SOURCE_PAGE,
                    "X-Requested-With": "XMLHttpRequest",
                },
                timeout=30,
            )
            if response.status_code != 200:
                raise RuntimeError(
                    "Teslastats inventory endpoint returned HTTP "
                    f"{response.status_code}"
                )

            vehicles, metadata = parse_inventory(response.text)
            metadata["fetched_at_utc"] = datetime.now(timezone.utc).isoformat()
            metadata["attempt"] = attempt
            log(f"Fetched and validated {len(vehicles)} Spain Model Y vehicles")
            return vehicles, metadata
        except Exception as exc:
            last_error = exc
            log(f"Fetch attempt {attempt}/3 failed: {type(exc).__name__}: {exc}")
            if attempt < 3:
                time.sleep(attempt * 2)

    raise RuntimeError(f"Could not fetch inventory after 3 attempts: {last_error}")


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Could not read state file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"State file {path} is not a JSON object")
    return data


def state_payload(vehicles: list[Vehicle], source_metadata: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "initialized": True,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "inventory_url": TESLA_URL,
        "source": {
            "name": "Teslastats Tesla Inventory",
            "page": SOURCE_PAGE,
            "api": SOURCE_API,
            "filters": {"country": "Spain", "model": "Model Y"},
            "last_fetch": source_metadata,
        },
        "vehicle_ids": [vehicle.vehicle_id for vehicle in vehicles],
        "vehicles": {
            vehicle.vehicle_id: asdict(vehicle) for vehicle in vehicles
        },
    }


def semantic_state(state: dict[str, Any]) -> dict[str, Any]:
    copy = json.loads(json.dumps(state))
    copy.pop("updated_at_utc", None)
    source = copy.get("source")
    if isinstance(source, dict):
        last_fetch = source.get("last_fetch")
        if isinstance(last_fetch, dict):
            last_fetch.pop("fetched_at_utc", None)
            last_fetch.pop("attempt", None)
    return copy


def escape_markdown_cell(value: str) -> str:
    return normalize_text(value).replace("|", r"\|") or "—"


def issue_marker(vehicle_ids: list[str]) -> str:
    raw = "\n".join(sorted(vehicle_ids)).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:20]


def github_request(
    path: str,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
) -> Any:
    token = os.environ.get("GITHUB_TOKEN", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    if not token or not repository:
        raise RuntimeError("GITHUB_TOKEN or GITHUB_REPOSITORY is missing")

    url = f"https://api.github.com/repos/{repository}{path}"
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "tesla-model-y-inventory-monitor",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        details = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub API returned HTTP {exc.code}: {details[:600]}"
        ) from exc


def alert_already_exists(marker: str) -> bool:
    issues = github_request("/issues?state=all&per_page=100")
    if not isinstance(issues, list):
        return False
    needle = f"<!-- tesla-alert:{marker} -->"
    return any(
        isinstance(issue, dict) and needle in str(issue.get("body") or "")
        for issue in issues
    )


def create_alert_issue(
    vehicles: list[Vehicle],
    total_count: int,
    first_scan: bool,
) -> None:
    marker = issue_marker([vehicle.vehicle_id for vehicle in vehicles])
    if alert_already_exists(marker):
        log(f"Alert issue {marker} already exists; skipping duplicate")
        return

    assignee = os.environ.get("TESLA_ALERT_ASSIGNEE", DEFAULT_ALERT_ASSIGNEE)
    event_text = "首次检查发现" if first_scan else "新出现"
    title = f"🚗 Tesla 西班牙 Model Y 有现车：{event_text} {len(vehicles)} 辆"

    rows = []
    for vehicle in vehicles[:25]:
        rows.append(
            "| "
            + " | ".join(
                [
                    escape_markdown_cell(vehicle.trim),
                    escape_markdown_cell(vehicle.paint),
                    escape_markdown_cell(vehicle.price),
                    escape_markdown_cell(vehicle.mileage),
                    escape_markdown_cell(vehicle.discount),
                    escape_markdown_cell(vehicle.location),
                    f"[查看]({vehicle.source_url})",
                ]
            )
            + " |"
        )

    extra = ""
    if len(vehicles) > 25:
        extra = f"\n\n另外还有 {len(vehicles) - 25} 辆未在表格中展开。"

    checked = datetime.now(timezone.utc).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )
    body = f"""@{assignee}，库存监控检测到西班牙 Model Y 现车。

- 本次{event_text}：**{len(vehicles)} 辆**
- 当前聚合器列出：**{total_count} 辆**
- 检查时间：{checked}
- [打开你指定的 Tesla 官方库存页面]({TESLA_URL})
- [打开西班牙 Model Y 聚合列表]({SOURCE_PAGE}?land=es&m=y)

| 版本 | 颜色 | 显示价格 | 里程 | 优惠 | 地点 | 车辆页 |
|---|---|---:|---:|---:|---|---|
{chr(10).join(rows)}{extra}

> 说明：Tesla 官方页面会拦截 GitHub 服务器，因此定时任务使用
> Teslastats 的公开 Spain + Model Y 数据作为触发源。聚合数据可能延迟，
> 车辆也可能刚被售出；请点击 Tesla 官方页面核实后再下单。

<!-- tesla-alert:{marker} -->
"""
    result = github_request(
        "/issues",
        method="POST",
        payload={"title": title, "body": body, "assignees": [assignee]},
    )
    issue_url = result.get("html_url") if isinstance(result, dict) else None
    log(f"Created alert issue: {issue_url or 'URL unavailable'}")


def set_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        return
    with open(output_path, "a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def main() -> int:
    dry_run = os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}
    state_path = Path(os.environ.get("TESLA_STATE_PATH", str(DEFAULT_STATE_PATH)))
    vehicles, source_metadata = fetch_inventory()

    log("Parsed rows:")
    for vehicle in vehicles:
        log(
            f"{vehicle.vehicle_id} | {vehicle.trim} | {vehicle.paint} | "
            f"{vehicle.price} | mileage={vehicle.mileage} | {vehicle.location}"
        )

    previous = load_state(state_path)
    initialized = bool(previous.get("initialized"))
    previous_ids = {str(value) for value in previous.get("vehicle_ids", []) if value}
    current_ids = {vehicle.vehicle_id for vehicle in vehicles}
    new_ids = current_ids if not initialized else current_ids - previous_ids
    new_vehicles = [
        vehicle for vehicle in vehicles if vehicle.vehicle_id in new_ids
    ]

    log(
        f"Comparison: initialized={initialized}, previous={len(previous_ids)}, "
        f"current={len(current_ids)}, new={len(new_ids)}, dry_run={dry_run}"
    )

    if dry_run:
        set_output("state_changed", "false")
        set_output("alert_created", "false")
        set_output("current_count", str(len(vehicles)))
        set_output("new_count", str(len(new_vehicles)))
        return 0

    alert_created = False
    if new_vehicles:
        create_alert_issue(
            new_vehicles,
            total_count=len(vehicles),
            first_scan=not initialized,
        )
        alert_created = True

    next_state = state_payload(vehicles, source_metadata)
    changed = semantic_state(previous) != semantic_state(next_state)
    if changed:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(
            json.dumps(next_state, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        log(f"Updated state file: {state_path}")
    else:
        log("Inventory and vehicle details are unchanged")

    set_output("state_changed", "true" if changed else "false")
    set_output("alert_created", "true" if alert_created else "false")
    set_output("current_count", str(len(vehicles)))
    set_output("new_count", str(len(new_vehicles)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise
    except Exception as exc:
        log(f"FATAL: {type(exc).__name__}: {exc}")
        raise
