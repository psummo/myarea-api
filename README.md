# myarea-api

Script Python per usare MyArea F2A (cartellino/timesheet) via API, senza browser.

## Uso

```bash
pip install -r requirements.txt
export MYAREA_USER=... MYAREA_PASS=...
python3 myarea_tm.py --help
```

Versione corrente: 0.1.0 (`python3 myarea_tm.py --version`).

Il JWT viene salvato in `~/.cache/myarea_tm_token.json` (override con `MYAREA_TOKEN_FILE`).

Funzioni principali: elenco richieste, creazione/cancellazione di SMWO, ROL e FERIE,
quadratura, apertura/chiusura timesheet, inizializzazione del mese.

## Variabili d'ambiente opzionali

| Variabile | Scopo |
|---|---|
| `MYAREA_TOKEN_FILE` | percorso della cache del JWT |
| `MYAREA_ANAG_ID` | ID anagrafica (di norma ricavato al login) |
| `MYAREA_APPROVER` | approvatore di fallback se l'API non lo restituisce |
| `MYAREA_CLIENT_ID` | ID cliente/azienda, solo se `checkUserInstancesDirect` non lo restituisce |

Gli ID delle causali (SMWO, ROL, FERIE) e quelli di anagrafica, azienda e approvatore
vengono letti da MyArea al login e alla prima richiesta: non serve configurare nulla.
