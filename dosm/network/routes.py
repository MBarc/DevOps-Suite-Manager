from __future__ import annotations

import csv
import io
import json
from collections import defaultdict
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from dosm.auth.deps import require_user
from dosm.db import get_session
from dosm.models import AuditLog, Host, NetworkPort, NetworkScan, NetworkScanResult, User
from dosm.network.executor import quick_check
from dosm.network.scanner import get_scan_activity, is_running, start_scan

router = APIRouter(prefix="/network")


def _templates(request: Request):
    return request.app.state.templates


# ── Port Library ─────────────────────────────────────────────────────────────


@router.get("/ports", response_class=HTMLResponse, include_in_schema=False)
async def port_library(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    ports = db.execute(select(NetworkPort).order_by(NetworkPort.port_number)).scalars().all()
    return _templates(request).TemplateResponse(
        request, "network/port_library.html", {"ports": ports, "user": user}
    )


@router.post("/ports/new", include_in_schema=False)
async def port_create(
    request: Request,
    port_number: int = Form(...),
    protocol: str = Form("tcp"),
    description: str = Form(...),
    is_default: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    try:
        port = NetworkPort(
            port_number=port_number,
            protocol=protocol,
            description=description.strip(),
            is_default=bool(is_default),
        )
        db.add(port)
        db.flush()
        db.add(AuditLog(actor_id=user.id, action="network.port.create", target=f"port:{port_number}"))
        db.commit()
    except IntegrityError:
        db.rollback()
        ports = db.execute(select(NetworkPort).order_by(NetworkPort.port_number)).scalars().all()
        return _templates(request).TemplateResponse(
            request,
            "network/port_library.html",
            {"ports": ports, "user": user, "error": f"Port {port_number} already exists."},
            status_code=400,
        )
    return RedirectResponse("/network/ports", status_code=303)


@router.post("/ports/{pid}/edit", include_in_schema=False)
async def port_edit(
    pid: int,
    description: str = Form(...),
    is_default: str = Form(""),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    port = db.get(NetworkPort, pid)
    if port is None:
        raise HTTPException(404)
    port.description = description.strip()
    port.is_default = bool(is_default)
    db.add(AuditLog(actor_id=user.id, action="network.port.update", target=f"port:{port.port_number}"))
    db.commit()
    return RedirectResponse("/network/ports", status_code=303)


@router.post("/ports/{pid}/delete", include_in_schema=False)
async def port_delete(
    pid: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    port = db.get(NetworkPort, pid)
    if port is None:
        raise HTTPException(404)
    pn = port.port_number
    db.delete(port)
    db.add(AuditLog(actor_id=user.id, action="network.port.delete", target=f"port:{pn}"))
    db.commit()
    return RedirectResponse("/network/ports", status_code=303)


# ── Port Checker ──────────────────────────────────────────────────────────────


@router.get("/port-checker", response_class=HTMLResponse, include_in_schema=False)
async def port_checker(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    hosts = db.execute(select(Host).order_by(Host.name)).scalars().all()
    return _templates(request).TemplateResponse(
        request, "network/port_checker.html", {"hosts": hosts, "user": user}
    )


@router.post("/port-checker/check", include_in_schema=False)
async def port_checker_run(
    request: Request,
    source_host_id: int = Form(...),
    dst_address: str = Form(...),
    port: int = Form(...),
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    source = db.get(Host, source_host_id)
    if source is None:
        return JSONResponse({"error": "Source host not found."}, status_code=404)

    cfg = request.app.state.config
    reachable, latency_ms, error_msg = await quick_check(cfg, db, source, dst_address.strip(), port)

    db.add(AuditLog(
        actor_id=user.id,
        action="network.check",
        target=f"host:{source_host_id}",
        details=f"dst={dst_address}:{port} reachable={reachable}",
    ))
    db.commit()

    return JSONResponse({
        "reachable": reachable,
        "latency_ms": latency_ms,
        "error_msg": error_msg,
        "source": source.name,
        "dst_address": dst_address,
        "port": port,
    })


# ── Network Map ───────────────────────────────────────────────────────────────


@router.get("/map", response_class=HTMLResponse, include_in_schema=False)
async def map_list(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    scans = db.execute(
        select(NetworkScan).order_by(NetworkScan.created_at.desc())
    ).scalars().all()
    scan_stats = []
    for s in scans:
        total = db.execute(
            select(NetworkScanResult).where(NetworkScanResult.scan_id == s.id)
        ).scalars().all()
        checked = [r for r in total if r.reachable is not None]
        reachable = [r for r in checked if r.reachable]
        scan_stats.append({
            "scan": s,
            "total": len(total),
            "checked": len(checked),
            "reachable": len(reachable),
            "running": is_running(s.id),
        })
    return _templates(request).TemplateResponse(
        request, "network/map_list.html", {"scans": scan_stats, "user": user}
    )


@router.get("/map/new", response_class=HTMLResponse, include_in_schema=False)
async def map_new_form(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    hosts = db.execute(select(Host).order_by(Host.name)).scalars().all()
    ports = db.execute(select(NetworkPort).order_by(NetworkPort.port_number)).scalars().all()
    return _templates(request).TemplateResponse(
        request,
        "network/map_new.html",
        {"hosts": hosts, "ports": ports, "user": user},
    )


@router.post("/map/new", include_in_schema=False)
async def map_create(
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    form = await request.form()
    title = (form.get("title") or "").strip()
    if not title:
        title = f"Scan {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')}"

    include_local = form.get("include_local") == "1"
    source_ids = [int(v) for v in form.getlist("source_host_ids") if v]
    dest_host_ids = [int(v) for v in form.getlist("dest_host_ids") if v]
    adhoc_raw = (form.get("adhoc_destinations") or "").strip()
    port_ids = [int(v) for v in form.getlist("port_ids") if v]

    if not source_ids and not include_local:
        hosts = db.execute(select(Host).order_by(Host.name)).scalars().all()
        ports = db.execute(select(NetworkPort).order_by(NetworkPort.port_number)).scalars().all()
        return _templates(request).TemplateResponse(
            request,
            "network/map_new.html",
            {"hosts": hosts, "ports": ports, "user": user, "error": "Select at least one source host."},
            status_code=400,
        )

    if not port_ids:
        hosts = db.execute(select(Host).order_by(Host.name)).scalars().all()
        ports = db.execute(select(NetworkPort).order_by(NetworkPort.port_number)).scalars().all()
        return _templates(request).TemplateResponse(
            request,
            "network/map_new.html",
            {"hosts": hosts, "ports": ports, "user": user, "error": "Select at least one port."},
            status_code=400,
        )

    # Build destination list
    destinations: list[dict] = []
    for hid in dest_host_ids:
        h = db.get(Host, hid)
        if h:
            destinations.append({"type": "inventory", "host_id": h.id, "address": h.hostname, "label": h.name})

    for line in adhoc_raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            label, _, address = line.partition("|")
            label, address = label.strip(), address.strip()
        else:
            label = address = line
        if address:
            destinations.append({"type": "adhoc", "host_id": None, "address": address, "label": label})

    if not destinations:
        hosts = db.execute(select(Host).order_by(Host.name)).scalars().all()
        ports = db.execute(select(NetworkPort).order_by(NetworkPort.port_number)).scalars().all()
        return _templates(request).TemplateResponse(
            request,
            "network/map_new.html",
            {"hosts": hosts, "ports": ports, "user": user, "error": "Add at least one destination."},
            status_code=400,
        )

    config = {"sources": source_ids, "destinations": destinations, "port_ids": port_ids, "local_source": include_local}
    scan = NetworkScan(
        title=title,
        status="pending",
        config_json=json.dumps(config),
        created_by_id=user.id,
    )
    db.add(scan)
    db.flush()
    _create_result_rows(db, scan.id, source_ids, destinations, port_ids, include_local=include_local)
    db.add(AuditLog(actor_id=user.id, action="network.scan.create", target=f"scan:{scan.id}", details=title))
    db.commit()

    cfg = request.app.state.config
    start_scan(scan.id, cfg)

    return RedirectResponse(f"/network/map/{scan.id}", status_code=303)


def _create_result_rows(
    db: Session,
    scan_id: int,
    source_ids: list[int],
    destinations: list[dict],
    port_ids: list[int],
    *,
    include_local: bool = False,
) -> None:
    ports = {p.id: p for p in db.execute(select(NetworkPort)).scalars()}
    if include_local:
        for dst in destinations:
            for pid in port_ids:
                p = ports.get(pid)
                if p is None:
                    continue
                db.add(NetworkScanResult(
                    scan_id=scan_id,
                    src_host_id=None,
                    src_label="DOSM Server",
                    dst_label=dst["label"],
                    dst_address=dst["address"],
                    port=p.port_number,
                    protocol=p.protocol,
                ))
    for src_id in source_ids:
        src = db.get(Host, src_id)
        if src is None:
            continue
        for dst in destinations:
            for pid in port_ids:
                p = ports.get(pid)
                if p is None:
                    continue
                db.add(NetworkScanResult(
                    scan_id=scan_id,
                    src_host_id=src_id,
                    src_label=src.name,
                    dst_label=dst["label"],
                    dst_address=dst["address"],
                    port=p.port_number,
                    protocol=p.protocol,
                ))


@router.get("/map/{sid}", response_class=HTMLResponse, include_in_schema=False)
async def map_detail(
    sid: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    scan = db.get(NetworkScan, sid)
    if scan is None:
        raise HTTPException(404)

    results = db.execute(
        select(NetworkScanResult).where(NetworkScanResult.scan_id == sid)
    ).scalars().all()

    graph_data = _build_graph(results)
    total = len(results)
    checked = sum(1 for r in results if r.reachable is not None)
    reachable = sum(1 for r in results if r.reachable)

    return _templates(request).TemplateResponse(
        request,
        "network/map_detail.html",
        {
            "scan": scan,
            "results": results,
            "graph_json": json.dumps(graph_data),
            "total": total,
            "checked": checked,
            "reachable": reachable,
            "running": is_running(sid),
            "user": user,
        },
    )


@router.get("/map/{sid}/status", include_in_schema=False)
async def map_status(
    sid: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    scan = db.get(NetworkScan, sid)
    if scan is None:
        raise HTTPException(404)
    results = db.execute(
        select(NetworkScanResult).where(NetworkScanResult.scan_id == sid)
    ).scalars().all()
    total = len(results)
    checked = sum(1 for r in results if r.reachable is not None)
    reachable = sum(1 for r in results if r.reachable)
    graph_data = _build_graph(results)

    activity = get_scan_activity(sid)

    # Prefer the in-memory value (updated before SSH connects, before DB commits).
    # Fall back to the most recently committed DB result for cross-process safety.
    last_check = activity["last"]
    if not last_check:
        last_result = db.execute(
            select(NetworkScanResult)
            .where(NetworkScanResult.scan_id == sid)
            .where(NetworkScanResult.checked_at.isnot(None))
            .order_by(desc(NetworkScanResult.checked_at))
            .limit(1)
        ).scalar_one_or_none()
        if last_result is not None:
            icon = "✓" if last_result.reachable else "✗"
            dst = last_result.dst_label or last_result.dst_address
            last_check = f"{last_result.src_label} → {dst}:{last_result.port} {icon}"

    return JSONResponse({
        "status": scan.status,
        "running": is_running(sid),
        "total": total,
        "checked": checked,
        "reachable": reachable,
        "unreachable": checked - reachable,
        "pending": total - checked,
        "graph": graph_data,
        "active_sources": activity["active"],
        "last_check": last_check,
    })


@router.post("/map/{sid}/rerun", include_in_schema=False)
async def map_rerun(
    sid: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    original = db.get(NetworkScan, sid)
    if original is None:
        raise HTTPException(404)

    config = json.loads(original.config_json)
    new_scan = NetworkScan(
        title=original.title,
        status="pending",
        config_json=original.config_json,
        created_by_id=user.id,
    )
    db.add(new_scan)
    db.flush()
    _create_result_rows(
        db,
        new_scan.id,
        config.get("sources", []),
        config.get("destinations", []),
        config.get("port_ids", []),
        include_local=config.get("local_source", False),
    )
    db.add(AuditLog(
        actor_id=user.id,
        action="network.scan.rerun",
        target=f"scan:{new_scan.id}",
        details=f"original={sid}",
    ))
    db.commit()

    cfg = request.app.state.config
    start_scan(new_scan.id, cfg)

    return RedirectResponse(f"/network/map/{new_scan.id}", status_code=303)


@router.post("/map/{sid}/rename", include_in_schema=False)
async def map_rename(
    sid: int,
    request: Request,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    body = await request.json()
    title = (body.get("title") or "").strip()
    if not title:
        return JSONResponse({"error": "Title cannot be empty"}, status_code=422)
    scan = db.get(NetworkScan, sid)
    if scan is None:
        raise HTTPException(404)
    scan.title = title
    db.add(AuditLog(actor_id=user.id, action="network.scan.rename", target=f"scan:{sid}", details=title))
    db.commit()
    return JSONResponse({"ok": True, "title": title})


@router.post("/map/{sid}/delete", include_in_schema=False)
async def map_delete(
    sid: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    scan = db.get(NetworkScan, sid)
    if scan is None:
        raise HTTPException(404)
    db.delete(scan)
    db.add(AuditLog(actor_id=user.id, action="network.scan.delete", target=f"scan:{sid}"))
    db.commit()
    return RedirectResponse("/network/map", status_code=303)


@router.get("/map/{sid}/export/json", include_in_schema=False)
async def map_export_json(
    sid: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    scan = db.get(NetworkScan, sid)
    if scan is None:
        raise HTTPException(404)
    results = db.execute(
        select(NetworkScanResult).where(NetworkScanResult.scan_id == sid)
    ).scalars().all()
    port_desc = {p.port_number: p.description for p in db.execute(select(NetworkPort)).scalars()}
    payload = {
        "scan": {
            "id": scan.id,
            "title": scan.title,
            "status": scan.status,
            "created_at": scan.created_at.isoformat(),
            "completed_at": scan.completed_at.isoformat() if scan.completed_at else None,
        },
        "results": [
            {
                "src": r.src_label,
                "dst": r.dst_label,
                "dst_address": r.dst_address,
                "port": r.port,
                "port_description": port_desc.get(r.port, ""),
                "protocol": r.protocol,
                "reachable": r.reachable,
                "latency_ms": r.latency_ms,
                "error_msg": r.error_msg,
            }
            for r in results
        ],
    }
    filename = f"scan_{sid}_{scan.title[:30].replace(' ', '_')}.json"
    return StreamingResponse(
        io.BytesIO(json.dumps(payload, indent=2).encode()),
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/map/{sid}/export/csv", include_in_schema=False)
async def map_export_csv(
    sid: int,
    db: Session = Depends(get_session),
    user: User = Depends(require_user),
):
    scan = db.get(NetworkScan, sid)
    if scan is None:
        raise HTTPException(404)
    results = db.execute(
        select(NetworkScanResult).where(NetworkScanResult.scan_id == sid)
    ).scalars().all()
    port_desc = {p.port_number: p.description for p in db.execute(select(NetworkPort)).scalars()}

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["source", "destination", "dst_address", "port", "port_description", "protocol", "reachable", "latency_ms", "error"])
    for r in results:
        writer.writerow([r.src_label, r.dst_label, r.dst_address, r.port,
                         port_desc.get(r.port, ""), r.protocol,
                         r.reachable, r.latency_ms, r.error_msg or ""])

    filename = f"scan_{sid}_{scan.title[:30].replace(' ', '_')}.csv"
    return StreamingResponse(
        io.BytesIO(buf.getvalue().encode()),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ── Graph builder ─────────────────────────────────────────────────────────────


def _build_graph(results: list[NetworkScanResult]) -> dict:
    """Build Cytoscape.js elements from scan results."""
    node_map: dict[str, dict] = {}
    edge_map: dict[tuple, dict] = {}

    def _get_or_add_node(label: str) -> dict:
        if label not in node_map:
            node_map[label] = {
                "id": f"n{len(node_map)}",
                "label": label,
                "is_source": False,
                "is_dest": False,
            }
        return node_map[label]

    for r in results:
        src = _get_or_add_node(r.src_label)
        src["is_source"] = True
        dst = _get_or_add_node(r.dst_label)
        dst["is_dest"] = True

        key = (r.src_label, r.dst_label)
        if key not in edge_map:
            edge_map[key] = {
                "id": f"e{len(edge_map)}",
                "source": src["id"],
                "target": dst["id"],
                "src_label": r.src_label,
                "dst_label": r.dst_label,
                "ports": [],
            }
        edge_map[key]["ports"].append({
            "port": r.port,
            "protocol": r.protocol,
            "reachable": r.reachable,
            "latency_ms": r.latency_ms,
            "error_msg": r.error_msg,
        })

    for edge in edge_map.values():
        ports = edge["ports"]
        checked = [p for p in ports if p["reachable"] is not None]
        ok = [p for p in checked if p["reachable"]]
        edge["reachable_count"] = len(ok)
        edge["checked_count"] = len(checked)
        edge["total_count"] = len(ports)

    return {
        "nodes": list(node_map.values()),
        "edges": list(edge_map.values()),
    }
