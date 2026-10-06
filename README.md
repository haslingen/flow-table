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

Programmet tar en 3280 × 2464-bild, hittar den runda bordsskivan (standard Ø300 mm), segmenterar den mörkare betongen och sparar följande i `captures/`:

- råbild
- markerad mätbild
- `measurements.csv` med area, ekvivalent diameter och största/minsta diameter

Ljuset och materialet avgör tröskelvärdet. Börja med `--dark-threshold 115` och justera vid behov, exempelvis `--dark-threshold 140`.
