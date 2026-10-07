#!/usr/bin/env python3
"""Local dashboard for the FlowTable camera measurement system."""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
import re
import subprocess
import sys

from flask import Flask, jsonify, render_template_string, request, send_from_directory


ROOT = Path(__file__).resolve().parent
CAPTURES = ROOT / "captures"
MEASUREMENTS = CAPTURES / "measurements.csv"
CAPTURE_SCRIPT = ROOT / "flowtable.py"
CALIBRATION = ROOT / "calibration.json"

app = Flask(__name__)

PAGE = """<!doctype html>
<html lang="sv">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
  <title>FlowTable</title>
  <style>
    :root { color-scheme: dark; --bg:#101418; --panel:#192027; --line:#2d3a45; --text:#eff5f8; --muted:#aebdc7; --accent:#68d5a6; }
    * { box-sizing:border-box } body { margin:0; font:16px system-ui,-apple-system,sans-serif; background:var(--bg); color:var(--text) }
    main { max-width:1200px; padding:28px 20px 48px; margin:auto } header { display:flex; gap:20px; align-items:center; justify-content:space-between; margin-bottom:24px }
    h1 { margin:0; font-size:28px } .sub { color:var(--muted); margin:4px 0 0 } button { border:0; border-radius:8px; padding:12px 18px; background:var(--accent); color:#082318; font-weight:700; font-size:15px; cursor:pointer }
    button:disabled { opacity:.6; cursor:wait } #status { min-height:24px; margin:0 0 16px; color:var(--muted) }
    .stats { display:grid; grid-template-columns:repeat(4,minmax(0,1fr)); gap:12px; margin-bottom:20px } .card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:16px }
    .label { color:var(--muted); font-size:13px; margin-bottom:5px } .value { font-size:25px; font-weight:700 } .unit { color:var(--muted); font-size:14px }
    .image-card { background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:14px } img { display:block; width:100%; max-height:680px; object-fit:contain; background:#050607; border-radius:6px }
    .empty { color:var(--muted); padding:64px 10px; text-align:center } footer { color:var(--muted); font-size:13px; margin-top:12px }
    @media (max-width:700px) { header { align-items:flex-start; flex-direction:column } .stats { grid-template-columns:repeat(2,minmax(0,1fr)) } }
  </style>
</head>
<body><main>
  <header><div><h1>FlowTable</h1><p class="sub">Senaste kameramätningen från slagbordet</p></div><div><button id="capture">Ta ny mätning</button></div></header>
  <section class="card" style="margin-bottom:16px"><form id="calibrate"><strong>Kalibrering</strong><div class="label" style="margin:6px 0 10px">Lämna den vita skivan tom. Ange dess största verkliga diameter och antal slag för ett test.</div><label for="diameter">Största diameter (mm)</label> <input id="diameter" type="number" min="1" step="0.1" value="{{ calibration.table_diameter_mm }}" required> <label for="strikes" style="margin-left:12px">Antal slag</label> <input id="strikes" type="number" min="1" step="1" value="{{ calibration.strikes_per_test }}" required> <button type="submit" style="margin-left:8px">Kalibrera</button></form></section>
  <p id="status">{{ status }}</p>
  {% if measurement %}
  <section class="stats">
    <div class="card"><div class="label">Area</div><div class="value">{{ measurement.area_cm2|round(1) }} <span class="unit">cm²</span></div></div>
    <div class="card"><div class="label">Ekvivalent diameter</div><div class="value">{{ measurement.equivalent_diameter_mm|round(1) }} <span class="unit">mm</span></div></div>
    <div class="card"><div class="label">Största diameter</div><div class="value">{{ measurement.diameter_max_mm|round(1) }} <span class="unit">mm</span></div></div>
    <div class="card"><div class="label">Minsta diameter</div><div class="value">{{ measurement.diameter_min_mm|round(1) }} <span class="unit">mm</span></div></div>
  </section>
  <section class="image-card"><img src="/captures/{{ image }}?v={{ measurement.captured_at_utc }}" alt="Senaste markerade kamerabild"><footer>Mätt {{ measurement.captured_at_display }} UTC</footer></section>
  {% else %}<section class="image-card empty">Ingen godkänd mätning finns ännu. Rikta kameran mot slagbordet och välj “Ta ny mätning”.</section>{% endif %}
  {% if calibration_image %}<section class="image-card" style="margin-top:16px"><h2 style="font-size:17px;margin:0 0 10px">Senaste kalibrering</h2><img src="/captures/{{ calibration_image }}?v={{ calibration_image }}" alt="Kalibreringsbild med blå referensring"><footer>Den blå ringen ska följa den vita skivans ytterkant.</footer></section>{% endif %}
</main><script>
const button=document.querySelector('#capture'), status=document.querySelector('#status'), calibration=document.querySelector('#calibrate');
button.addEventListener('click', async () => { button.disabled=true; status.textContent='Tar bild och beräknar utbredningen…'; try { const r=await fetch('/capture',{method:'POST'}); const data=await r.json(); if (!r.ok) throw new Error(data.error); location.reload(); } catch(e) { status.textContent='Mätningen misslyckades: '+e.message; button.disabled=false; } });
calibration.addEventListener('submit', async (event) => { event.preventDefault(); const value=Number(document.querySelector('#diameter').value), strikes=Number(document.querySelector('#strikes').value); if (!(value>0 && Number.isInteger(strikes) && strikes>0)) return; status.textContent='Tar kalibreringsbild…'; try { const r=await fetch('/calibrate',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({table_diameter_mm:value,strikes_per_test:strikes})}); const data=await r.json(); if (!r.ok) throw new Error(data.error); location.reload(); } catch(e) { status.textContent='Kalibreringen misslyckades: '+e.message; } });
</script></body></html>"""


def latest_measurement() -> dict[str, object] | None:
    if not MEASUREMENTS.exists():
        return None
    with MEASUREMENTS.open(newline="") as file:
        rows = list(csv.DictReader(file))
    if not rows:
        return None
    row = rows[-1]
    result = {key: float(row[key]) for key in row if key != "captured_at_utc"}
    result["captured_at_utc"] = row["captured_at_utc"]
    result["captured_at_display"] = datetime.strptime(row["captured_at_utc"], "%Y%m%dT%H%M%SZ").strftime("%Y-%m-%d %H:%M:%S")
    return result


def measured_image(measurement: dict[str, object] | None) -> str | None:
    if not measurement:
        return None
    candidate = f"{measurement['captured_at_utc']}_measured.jpg"
    return candidate if (CAPTURES / candidate).is_file() else None


def latest_calibration_image() -> str | None:
    images = sorted(CAPTURES.glob("*_calibration.jpg"))
    return images[-1].name if images else None


def readable_capture_error(output: str) -> str:
    """Keep camera driver diagnostics out of the browser-facing error message."""
    clean = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", output)
    marker = "measurement failed:"
    if marker in clean:
        return clean[clean.index(marker) + len(marker) :].strip()
    return "Kamerabilden kunde inte analyseras. Kontrollera att slagbordet syns helt i bilden."


def calibration_settings() -> dict[str, float]:
    if not CALIBRATION.exists():
        return {"table_diameter_mm": 297.0, "strikes_per_test": 15}
    try:
        import json
        with CALIBRATION.open() as file:
            data = json.load(file)
            return {"table_diameter_mm": float(data["table_diameter_mm"]), "strikes_per_test": int(data.get("strikes_per_test", 15))}
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return {"table_diameter_mm": 297.0, "strikes_per_test": 15}


@app.get("/")
def dashboard():
    measurement = latest_measurement()
    return render_template_string(
        PAGE,
        measurement=measurement,
        image=measured_image(measurement),
        calibration_image=latest_calibration_image(),
        calibration=calibration_settings(),
        status=("Ingen betong upptäcktes — den kalibrerade skivan visas." if measurement and measurement["area_cm2"] == 0 else "Klar") if measurement else "Väntar på första mätningen",
    )


@app.get("/captures/<path:filename>")
def capture_file(filename: str):
    return send_from_directory(CAPTURES, filename)


@app.post("/capture")
def capture():
    result = subprocess.run(
        [sys.executable, str(CAPTURE_SCRIPT)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
    )
    if result.returncode:
        return jsonify(error=readable_capture_error(result.stdout)), 422
    return jsonify(ok=True, output=result.stdout)


@app.post("/calibrate")
def calibrate():
    data = request.get_json(silent=True) or {}
    try:
        diameter = float(data["table_diameter_mm"])
        strikes = int(data.get("strikes_per_test", 15))
    except (KeyError, TypeError, ValueError):
        return jsonify(error="Ange en giltig diameter i millimeter."), 400
    if diameter <= 0 or strikes <= 0:
        return jsonify(error="Diametern och antal slag måste vara större än noll."), 400
    result = subprocess.run(
        [sys.executable, str(CAPTURE_SCRIPT), "--calibrate", "--table-diameter-mm", str(diameter), "--strikes", str(strikes)],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
    )
    if result.returncode:
        return jsonify(error=readable_capture_error(result.stdout)), 422
    return jsonify(ok=True, output=result.stdout)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8767)
