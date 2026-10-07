# FlowTable

Kamerabaserad mätning av betongens utbredning på ett runt slagbord.

## Första installationen på Raspberry Pi

```bash
sudo apt update
sudo apt install -y python3-opencv python3-picamera2
```

Kopiera `flowtable.py` till Pi:n och kör:

```bash
python3 flowtable.py
```

Programmet tar en 3280 × 2464-bild, och använder den kalibrerade vita referensskivan som **297 × 297 mm**. Referensskivans ellips i kamerabilden omvandlas till en cirkel i bordets plan, så att perspektivförvrängningen kompenseras före segmenteringen av den mörkare betongen. Det sparar följande i `captures/`:

- råbild
- markerad mätbild
- `measurements.csv` med area, ekvivalent diameter och största/minsta diameter

Ljuset och materialet avgör tröskelvärdet. Börja med `--dark-threshold 115` och justera vid behov, exempelvis `--dark-threshold 140`.

## Webbapp på Pi:n

Webbappen visar senaste markerade bild och mätvärden samt kan ta en ny mätning från en mobil eller dator på samma nätverk.

```bash
sudo install -m 644 deploy/flowtable-web.service /etc/systemd/system/flowtable-web.service
sudo systemctl daemon-reload
sudo systemctl enable --now flowtable-web
```

Öppna sedan `http://lasersensor:8767` eller `http://192.168.1.211:8767` på det lokala nätverket.
