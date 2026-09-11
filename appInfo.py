# V10.5.8 V3 - GPS LIVE: base GPS V10.5.4 + ETA OSRM V2
import streamlit as st
import pandas as pd
import urllib.parse
import os
import base64
import json
import time
from datetime import datetime, date
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
import requests
from io import BytesIO
import gspread
from google.oauth2.service_account import Credentials

# GPS smartphone - prima fase VanGo Test.
try:
    from streamlit_js_eval import get_geolocation, streamlit_js_eval
except ImportError:
    get_geolocation = None


# Configurazione Pagina
st.set_page_config(
    page_title="VanGo - Giro Consegne",
    page_icon="🚐",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Stati consegna: definiti PRIMA di qualsiasi uso nel codice.
VERSIONE_VANGO = "V10_5_8_V3_TEST_4.py"

# DATABASE GOOGLE SHEETS DEDICATO A QUESTA ISTANZA VANGO.
# Non usare open() per titolo: ogni ramo deve essere isolato dal database dell'altro ramo.
VANGO_SPREADSHEET_ID = "1OMdvDb7Ttgj6lZxr3kinLqv3Gry130RSXo7xnZaLyRE"
STATO_DA_FARE = "⚪ DA CONSEGNARE"
STATO_FATTO = "🟢 FATTO"
STATO_PARZIALE = "🟡 PARZIALE"
STATO_RESPINTO = "🔴 RESPINTO"
STATI_CONSEGNA = [STATO_DA_FARE, STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO]

# Sessione persistente per singolo browser/dispositivo
# Richiede: streamlit-local-storage
# Il token viene salvato nel localStorage del singolo browser.
STORAGE_KEY = "vango_session"
SESSIONE_MAX_GIORNI = 365

try:
    from streamlit_local_storage import LocalStorage
except ImportError:
    LocalStorage = None

local_storage = LocalStorage() if LocalStorage is not None else None

def _cookie_secret():
    """Segreto stabile per firmare il token salvato nel browser."""
    try:
        secret = st.secrets.get("SESSION_COOKIE_SECRET")
        if secret:
            return str(secret)
    except Exception:
        pass

    try:
        private_key = st.secrets["gcp_service_account"]["private_key"]
        if private_key:
            return str(private_key)
    except Exception:
        pass

    return "VANGO_SESSION_SECRET_CAMBIARE_IN_STREAMLIT_SECRETS"

SESSION_SECRET = _cookie_secret()

def _firma_sessione(payload):
    import hashlib
    import hmac
    return hmac.new(
        SESSION_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

def genera_token_sessione(utente):
    import base64
    import json
    import time

    dati = {
        "utente": str(utente),
        "exp": int(time.time()) + SESSIONE_MAX_GIORNI * 24 * 60 * 60
    }

    payload = base64.urlsafe_b64encode(
        json.dumps(dati, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode("ascii").rstrip("=")

    return f"{payload}.{_firma_sessione(payload)}"

def leggi_sessione_persistente():
    if local_storage is None:
        return None

    try:
        valore = local_storage.getItem(STORAGE_KEY)
        if not valore or not isinstance(valore, str) or "." not in valore:
            return None

        payload, firma = valore.rsplit(".", 1)

        import hmac
        if not hmac.compare_digest(firma, _firma_sessione(payload)):
            return None

        import base64
        import json
        import time

        padding = "=" * (-len(payload) % 4)
        dati = json.loads(
            base64.urlsafe_b64decode(
                (payload + padding).encode("ascii")
            ).decode("utf-8")
        )

        if int(dati.get("exp", 0)) <= int(time.time()):
            return None

        utente = str(dati.get("utente", "")).strip()
        return utente or None

    except Exception:
        return None

def salva_sessione_persistente(utente):
    if local_storage is None or not utente:
        return False

    try:
        local_storage.setItem(
            STORAGE_KEY,
            genera_token_sessione(utente)
        )
        return True
    except Exception:
        return False

def elimina_sessione_persistente():
    if local_storage is None:
        return

    try:
        local_storage.deleteItem(STORAGE_KEY)
    except Exception:
        pass

# ==========================================
# OTTIMIZZATORE GIRO FREE - OpenStreetMap + OSRM + OR-Tools
# ==========================================
# Nessuna Route Optimization API Google e nessuna Google Geocoding API.
# La geocodifica usa Nominatim/OpenStreetMap; il routing usa OSRM.
# ORA viene volutamente IGNORATA dall'ottimizzazione.
DEPOSITO_VANGO = "Dolciaria Acquaviva, Via Enrico Fermi, 10, Burago di Molgora, MB, Italia"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
PHOTON_URL = "https://photon.komoot.io/api/"
ARCGIS_GEOCODER_URL = "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/findAddressCandidates"
OSRM_TABLE_URL = "https://router.project-osrm.org/table/v1/driving"

# Coordinate verificate per il deposito fisso di VanGo.
# In questo modo il deposito non dipende dalla geocodifica pubblica.
COORDINATE_DEPOSITO_VANGO = (45.59085, 9.384842)
# Tempo medio fisso di parcheggio + scarico per ogni fermata.
MINUTI_SERVIZIO_PER_FERMATA = 12

def _geocodifica_free(indirizzo):
    """Geocodifica gratuita con piu' fornitori e protezione dai limiti.

    Ordine:
    1) Nominatim/OpenStreetMap con query strutturata e retry;
    2) Photon/OpenStreetMap come secondo motore OSM;
    3) ArcGIS World Geocoder come ulteriore fallback pubblico.

    La cache per 30 giorni evita di ripetere le stesse richieste.
    """
    indirizzo = str(indirizzo or "").strip()
    if not indirizzo:
        return None

    headers = {
        "User-Agent": "VanGo-GiroConsegne/2.2 (route optimizer; contact: vango)"
    }

    # Normalizza leggermente l'indirizzo per aumentare la compatibilita'.
    indirizzo_base = indirizzo.replace(", Italia", "").replace(", Italy", "").strip()
    query_varianti = list(dict.fromkeys([
        indirizzo,
        indirizzo_base,
    ]))

    # ------------------------------------------------------------
    # 1) NOMINATIM - un'unica richiesta per variante, rispettando
    #    il limite pubblico di circa 1 richiesta/secondo.
    # ------------------------------------------------------------
    for n, query in enumerate(query_varianti):
        try:
            if n > 0:
                time.sleep(1.2)
            params = {
                "q": query,
                "format": "jsonv2",
                "limit": 1,
                "countrycodes": "it",
                "addressdetails": 1,
            }
            response = requests.get(
                NOMINATIM_URL, params=params, headers=headers, timeout=12
            )
            if response.status_code == 200:
                risultati = response.json()
                if risultati:
                    return {
                        "lat": float(risultati[0]["lat"]),
                        "lon": float(risultati[0]["lon"]),
                        "display_name": risultati[0].get("display_name", query),
                        "provider": "Nominatim",
                    }
        except Exception:
            pass

    # ------------------------------------------------------------
    # 2) PHOTON - secondo motore basato su OpenStreetMap.
    #    Proviamo la stringa completa e quella semplificata.
    # ------------------------------------------------------------
    for query in query_varianti:
        try:
            response = requests.get(
                PHOTON_URL,
                params={"q": query, "limit": 1, "lang": "it"},
                headers=headers,
                timeout=12,
            )
            if response.status_code == 200:
                features = response.json().get("features", [])
                if features:
                    coords = features[0].get("geometry", {}).get("coordinates", [])
                    if len(coords) >= 2:
                        props = features[0].get("properties", {})
                        return {
                            "lat": float(coords[1]),
                            "lon": float(coords[0]),
                            "display_name": props.get("name", query),
                            "provider": "Photon",
                        }
        except Exception:
            pass

    # ------------------------------------------------------------
    # 3) ARCGIS - fallback ulteriore senza usare Google Maps API.
    #    L'endpoint pubblico e' usato solo per trovare la posizione.
    # ------------------------------------------------------------
    try:
        response = requests.get(
            ARCGIS_GEOCODER_URL,
            params={
                "SingleLine": indirizzo_base,
                "countryCode": "ITA",
                "maxLocations": 1,
                "outFields": "Match_addr,Addr_type",
                "forStorage": "false",
                "f": "json",
            },
            headers=headers,
            timeout=12,
        )
        if response.status_code == 200:
            candidati = response.json().get("candidates", [])
            if candidati:
                candidato = candidati[0]
                posizione = candidato.get("location", {})
                x = posizione.get("x")
                y = posizione.get("y")
                if x is not None and y is not None:
                    return {
                        "lat": float(y),
                        "lon": float(x),
                        "display_name": candidato.get("address", indirizzo),
                        "provider": "ArcGIS",
                    }
    except Exception:
        pass

    return None


def _indirizzo_riga(row):
    via = str(row.get("VIA", "")).strip()
    comune = str(row.get("COMUNE", "")).strip()
    return f"{via}, {comune}, Italia" if via and comune else (via or comune)


def _parse_coordinate(valore):
    """Legge una coordinata salvata in H nel formato 'lat, lon'."""
    if valore is None or (isinstance(valore, float) and pd.isna(valore)):
        return None
    testo = str(valore).strip()
    if not testo or testo.lower() in {"nan", "none", "null"}:
        return None
    try:
        parti = [x.strip().replace(",", ".") for x in testo.replace(";", ",").split(",")]
        if len(parti) != 2:
            return None
        lat, lon = float(parti[0]), float(parti[1])
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            return None
        return (lat, lon)
    except Exception:
        return None


def _coordinate_riga_db(row):
    """Recupera le coordinate già salvate nel database clienti (colonna H)."""
    return _parse_coordinate(row.get("COORDINATE", ""))


def _trova_coordinate_nel_db(row_giro, df_db):
    """Trova le coordinate del cliente nel DB usando cliente + via + comune."""
    if df_db is None or df_db.empty or "COORDINATE" not in df_db.columns:
        return None

    cliente = str(row_giro.get("CLIENTE", "")).strip().casefold()
    via = str(row_giro.get("VIA", "")).strip().casefold()
    comune = str(row_giro.get("COMUNE", "")).strip().casefold()

    # Prima corrispondenza precisa su CLIENTE + VIA + COMUNE.
    for _, r in df_db.iterrows():
        if (str(r.get("CLIENTE", "")).strip().casefold() == cliente and
            str(r.get("VIA", "")).strip().casefold() == via and
            str(r.get("COMUNE", "")).strip().casefold() == comune):
            coord = _coordinate_riga_db(r)
            if coord:
                return coord

    return None


def _aggiorna_coordinate_db(df_db, df_giro, coordinate_nuove):
    """Aggiorna in memoria le coordinate del DB per le fermate appena geocodificate."""
    if df_db is None or df_db.empty or "COORDINATE" not in df_db.columns:
        return df_db
    risultato = df_db.copy()
    for _, r in df_giro.iterrows():
        chiave_cliente = str(r.get("CLIENTE", "")).strip().casefold()
        chiave_via = str(r.get("VIA", "")).strip().casefold()
        chiave_comune = str(r.get("COMUNE", "")).strip().casefold()
        coord = coordinate_nuove.get((chiave_cliente, chiave_via, chiave_comune))
        if coord:
            mask = (
                risultato["CLIENTE"].astype(str).str.strip().str.casefold().eq(chiave_cliente) &
                risultato["VIA"].astype(str).str.strip().str.casefold().eq(chiave_via) &
                risultato["COMUNE"].astype(str).str.strip().str.casefold().eq(chiave_comune)
            )
            risultato.loc[mask, "COORDINATE"] = f"{coord[0]:.7f}, {coord[1]:.7f}"
    return risultato


def _richiedi_matrice_osrm(coordinate):
    """Restituisce matrici distanze (m) e durate (s) tra tutte le coordinate."""
    if not coordinate:
        raise ValueError("Nessuna coordinata disponibile per il calcolo del percorso.")
    coord_string = ";".join(f"{lon},{lat}" for lat, lon in coordinate)
    url = f"{OSRM_TABLE_URL}/{coord_string}"
    params = {"annotations": "distance,duration"}
    response = requests.get(url, params=params, timeout=45)
    response.raise_for_status()
    dati = response.json()
    if dati.get("code") != "Ok":
        raise RuntimeError(f"OSRM non ha restituito una matrice valida: {dati.get('message', dati.get('code', 'errore sconosciuto'))}")
    distanze = dati.get("distances")
    durate = dati.get("durations")
    if not distanze or not durate:
        raise RuntimeError("OSRM ha restituito una matrice vuota.")
    return distanze, durate


def _percorso_da_indici(indici, distanze, durate):
    totale_m = 0.0
    totale_s = 0.0
    for a, b in zip(indici[:-1], indici[1:]):
        d = distanze[a][b]
        t = durate[a][b]
        if d is None or t is None:
            raise RuntimeError("Esiste una tratta stradale non raggiungibile nella matrice OSRM.")
        totale_m += float(d)
        totale_s += float(t)
    return totale_m, totale_s

def calcola_metriche_giro_campo(df_giro, df_db):
    """Calcola KM e tempo della parte di giro ancora da fare in CAMPO.

    L'origine e' l'ultima consegna gia' gestita; se non ce n'e' una, parte dal
    deposito. Include tutte le fermate ancora da consegnare nell'ordine corrente
    e il rientro al deposito. Non modifica il giro e non riottimizza nulla.
    """
    if df_giro is None or df_giro.empty:
        return {"km": 0.0, "minuti": 0.0}

    df = df_giro.copy().reset_index(drop=True)
    # Protezione: alcuni DataFrame prodotti dall'ottimizzatore possono contenere
    # colonne duplicate. Con colonne duplicate, df.at[...] puo' generare
    # "TypeError" quando assegniamo un singolo valore. Manteniamo la prima
    # occorrenza di ogni nome, evitando di interrompere APPLICA GIRO OTTIMIZZATO.
    if df.columns.duplicated().any():
        df = df.loc[:, ~df.columns.duplicated(keep="first")].copy()
    if "STATO" not in df.columns:
        df["STATO"] = STATO_DA_FARE
    df["STATO"] = df["STATO"].fillna("").astype(str)

    stati_gestiti = [STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO]
    pendenti = df[~df["STATO"].isin(stati_gestiti)].copy().reset_index(drop=True)

    gestiti = df[df["STATO"].isin(stati_gestiti)]
    if not gestiti.empty:
        ultima_gestita = gestiti.iloc[-1]
        origine = _trova_coordinate_nel_db(ultima_gestita, df_db)
        if origine is None:
            origine = COORDINATE_DEPOSITO_VANGO
    else:
        origine = COORDINATE_DEPOSITO_VANGO

    # A fine consegne resta comunque il rientro dall'ultima fermata alla sede.
    if pendenti.empty:
        coordinate = [origine, COORDINATE_DEPOSITO_VANGO]
        try:
            distanze, durate = _richiedi_matrice_osrm(coordinate)
            d = distanze[0][1]
            t = durate[0][1]
            if d is None or t is None:
                return {"km": 0.0, "minuti": 0.0}
            return {"km": float(d) / 1000.0, "minuti": float(t) / 60.0}
        except Exception:
            return {"km": 0.0, "minuti": 0.0}

    coordinate = [origine]
    for _, row in pendenti.iterrows():
        coord = _trova_coordinate_nel_db(row, df_db)
        if coord is None:
            return None
        coordinate.append(coord)
    coordinate.append(COORDINATE_DEPOSITO_VANGO)

    distanze, durate = _richiedi_matrice_osrm(coordinate)
    ordine = list(range(len(coordinate)))
    km, secondi = _percorso_da_indici(ordine, distanze, durate)
    return {"km": km / 1000.0, "minuti": secondi / 60.0}


def calcola_metriche_giro_corrente(df_giro, df_db):
    """Calcola KM e tempo del giro attualmente salvato, senza riottimizzarlo.

    Usa esattamente l'ordine corrente delle fermate + deposito di partenza/fine.
    Non modifica il motore dell'ottimizzatore e non modifica l'ordine del giro.
    Restituisce None se manca almeno una coordinata nel DB.
    """
    if df_giro is None or df_giro.empty:
        return None

    coordinate = [COORDINATE_DEPOSITO_VANGO]
    for _, row in df_giro.reset_index(drop=True).iterrows():
        coord = _trova_coordinate_nel_db(row, df_db)
        if coord is None:
            return None
        coordinate.append(coord)
    coordinate.append(COORDINATE_DEPOSITO_VANGO)

    distanze, durate = _richiedi_matrice_osrm(coordinate)
    ordine = list(range(len(coordinate)))
    km, secondi = _percorso_da_indici(ordine, distanze, durate)
    return {
        "km": km / 1000.0,
        "minuti": secondi / 60.0,
    }



def _firma_ordine_giro(df):
    """Firma stabile dell'ordine corrente, per non mostrare metriche ORARI vecchie."""
    if df is None or df.empty:
        return tuple()
    cols = ["CLIENTE", "COMUNE", "VIA", "ORA"]
    return tuple(
        tuple(str(row.get(c, "")).strip() for c in cols)
        for _, row in df.reset_index(drop=True).iterrows()
    )



def _numero_minuti_cumulativi(valore):
    """Converte in float un valore MIN_PREVISTI_CUMULATIVI, oppure None."""
    try:
        if valore is None or (isinstance(valore, float) and pd.isna(valore)):
            return None
        testo = str(valore).strip().replace(',', '.')
        if not testo or testo.lower() in ("nan", "none", "nat"):
            return None
        return float(testo)
    except Exception:
        return None


def _metodo_previsione_usa_orari():
    """Stabilisce se la previsione cumulativa deve applicare le attese ORARI."""
    previsione = st.session_state.get("previsione_giro") or {}
    metodo = str(previsione.get("metodo", "")).upper()
    modalita = str(st.session_state.get("modalita_ottimizzazione", "")).upper()
    return metodo.startswith("ORARI") or "ORARI" in modalita


def _calcola_previsione_cumulativa_giro(df_giro, df_db):
    """Calcola la previsione cumulativa fermata-per-fermata sull'ordine reale."""
    if df_giro is None or df_giro.empty:
        return df_giro.copy() if df_giro is not None else pd.DataFrame(), None

    df = df_giro.copy().reset_index(drop=True)
    if "STATO" not in df.columns:
        df["STATO"] = STATO_DA_FARE
    df["STATO"] = df["STATO"].fillna("").astype(str)
    # Le colonne possono arrivare da Google Sheets con dtype string/Arrow.
    # Devono essere numeriche per poter assegnare i minuti float.
    if "MIN_TRATTA_PREVISTA" in df.columns:
        df["MIN_TRATTA_PREVISTA"] = pd.to_numeric(
            df["MIN_TRATTA_PREVISTA"], errors="coerce"
        ).astype("float64")
    else:
        df["MIN_TRATTA_PREVISTA"] = pd.Series(
            float("nan"), index=df.index, dtype="float64"
        )

    if "MIN_PREVISTI_CUMULATIVI" in df.columns:
        df["MIN_PREVISTI_CUMULATIVI"] = pd.to_numeric(
            df["MIN_PREVISTI_CUMULATIVI"], errors="coerce"
        ).astype("float64")
    else:
        df["MIN_PREVISTI_CUMULATIVI"] = pd.Series(
            float("nan"), index=df.index, dtype="float64"
        )

    stati_gestiti = [STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO]
    gestiti_idx = [i for i in range(len(df)) if df.iloc[i]["STATO"].strip() in stati_gestiti]
    pendenti_idx = [i for i in range(len(df)) if df.iloc[i]["STATO"].strip() not in stati_gestiti]

    if not pendenti_idx:
        valori = [_numero_minuti_cumulativi(df.iloc[i].get("MIN_PREVISTI_CUMULATIVI")) for i in range(len(df))]
        validi = [v for v in valori if v is not None]
        return df, (validi[-1] + MINUTI_SERVIZIO_PER_FERMATA if validi else None)

    ultimo_gestito_idx = gestiti_idx[-1] if gestiti_idx else None
    base_cumulativa = 0.0
    origine = COORDINATE_DEPOSITO_VANGO
    servizio_precedente = False

    if ultimo_gestito_idx is not None:
        base_salvata = _numero_minuti_cumulativi(df.iloc[ultimo_gestito_idx].get("MIN_PREVISTI_CUMULATIVI"))
        coord_ultima = _trova_coordinate_nel_db(df.iloc[ultimo_gestito_idx], df_db) if base_salvata is not None else None
        if base_salvata is not None and coord_ultima is not None:
            origine = coord_ultima
            base_cumulativa = float(base_salvata)
            servizio_precedente = True

    coordinate = [origine]
    for idx in pendenti_idx:
        coord = _trova_coordinate_nel_db(df.iloc[idx], df_db)
        if coord is None:
            return df, None
        coordinate.append(coord)
    coordinate.append(COORDINATE_DEPOSITO_VANGO)

    try:
        _, durate = _richiedi_matrice_osrm(coordinate)
    except Exception:
        return df, None

    usa_orari = _metodo_previsione_usa_orari()
    ora_partenza = _ora_partenza_reale_minuti()
    tempo_cumulativo = float(base_cumulativa)

    for pos, idx in enumerate(pendenti_idx, start=1):
        if servizio_precedente:
            tempo_cumulativo += float(MINUTI_SERVIZIO_PER_FERMATA)
        viaggio = durate[pos - 1][pos]
        if viaggio is None:
            return df, None
        minuti_tratta = float(viaggio) / 60.0
        # Tempo previsto della singola tratta: origine (sede oppure ultimo cliente gestito)
        # -> cliente corrente. Questo valore resta visibile separatamente dal cumulativo.
        df.loc[idx, "MIN_TRATTA_PREVISTA"] = round(minuti_tratta, 1)
        tempo_cumulativo += minuti_tratta

        if usa_orari and ora_partenza is not None:
            apertura = _parse_orario_apertura(df.iloc[idx].get("ORA", ""))
            if apertura is not None:
                ora_arrivo = float(ora_partenza) + tempo_cumulativo
                tempo_cumulativo += max(0.0, float(apertura) - ora_arrivo)

        df.loc[idx, "MIN_PREVISTI_CUMULATIVI"] = round(tempo_cumulativo, 1)
        servizio_precedente = True

    tempo_fine = tempo_cumulativo + float(MINUTI_SERVIZIO_PER_FERMATA)
    rientro = durate[len(pendenti_idx)][len(pendenti_idx) + 1]
    if rientro is None:
        return df, None
    tempo_fine += float(rientro) / 60.0
    return df, round(tempo_fine, 1)


def _assicura_previsione_cumulativa_giro(salva=True):
    """Ricalcola OSRM solo quando cambia l'ordine reale delle fermate."""
    df = st.session_state.get("giro_corrente")
    if df is None or df.empty:
        return False

    firma = _firma_ordine_giro(df)
    previsione = st.session_state.get("previsione_giro") or {}
    firma_salvata = previsione.get("firma_ordine_cumulativa")
    valori_mancanti = "MIN_PREVISTI_CUMULATIVI" not in df.columns or any(
        _numero_minuti_cumulativi(v) is None for v in df["MIN_PREVISTI_CUMULATIVI"].tolist()
    )
    if firma_salvata == firma and not valori_mancanti:
        return False

    nuovo_df, totale = _calcola_previsione_cumulativa_giro(df, st.session_state.get("db_clienti"))
    if totale is None:
        return False

    st.session_state.giro_corrente = nuovo_df
    st.session_state.previsione_giro = {
        "minuti": float(totale),
        "metodo": str(previsione.get("metodo") or "TEMPO CUMULATIVO OSRM"),
        "firma": firma,
        "firma_ordine_cumulativa": firma,
    }
    if salva and st.session_state.get("utente_corrente"):
        salva_stato_giro_persistente(st.session_state.utente_corrente)
        salva_giro_utente_su_sheets(st.session_state.utente_corrente, nuovo_df)
    return True

def _gruppo_da_zona(valore):
    """Converte la ZONA numerica in un macro-gruppo.

    Esempi: 100-199 -> 1, 200-299 -> 2, 300-399 -> 3.
    Se ZONA non e' interpretabile come numero, la fermata resta libera.
    """
    try:
        testo = str(valore).strip().replace(',', '.')
        if not testo:
            return None
        numero = int(float(testo))
        if numero < 100:
            return None
        return numero // 100
    except (TypeError, ValueError):
        return None


def _normalizza_chiave_testo(valore):
    """Normalizza testi per confronti robusti tra GiroAttivo e Foglio1."""
    import unicodedata, re
    x = "" if valore is None else str(valore)
    x = unicodedata.normalize("NFKD", x).encode("ascii", "ignore").decode("ascii")
    x = x.casefold().strip()
    x = re.sub(r"[.,;:/\\\-]+", " ", x)
    x = re.sub(r"\s+", " ", x)
    return x

def _gruppi_fermate(df_giro, df_db):
    """Recupera il macro-gruppo ZONA in modo robusto dal Foglio1.

    Prima prova CLIENTE + VIA + COMUNE. Se non trova la riga, prova VIA +
    COMUNE. Questo evita che una piccola differenza nel nome cliente faccia
    perdere la ZONA e quindi disattivi di fatto il raggruppamento.
    """
    if df_giro is None or df_giro.empty:
        return []
    if df_db is None or df_db.empty or "ZONA" not in df_db.columns:
        return [None] * len(df_giro)

    db = df_db.copy()
    for col in ["CLIENTE", "VIA", "COMUNE"]:
        if col in db.columns:
            db[f"__K_{col}"] = db[col].map(_normalizza_chiave_testo)

    risultati = []
    for _, row in df_giro.iterrows():
        # Se ZONA e' gia' presente nel giro, e' la fonte piu' affidabile.
        valore = row.get("ZONA", None)
        if valore is not None and str(valore).strip() not in ("", "nan", "None"):
            risultati.append(_gruppo_da_zona(valore))
            continue

        cliente = _normalizza_chiave_testo(row.get("CLIENTE", ""))
        via = _normalizza_chiave_testo(row.get("VIA", ""))
        comune = _normalizza_chiave_testo(row.get("COMUNE", ""))

        valore_trovato = None
        # 1) Chiave completa.
        if all(c in db.columns for c in ["__K_CLIENTE", "__K_VIA", "__K_COMUNE"]):
            mask = (db["__K_CLIENTE"].eq(cliente) & db["__K_VIA"].eq(via) & db["__K_COMUNE"].eq(comune))
            candidati = db.loc[mask, "ZONA"]
            if not candidati.empty:
                valore_trovato = candidati.iloc[0]

        # 2) Fallback fondamentale: VIA + COMUNE.
        if valore_trovato is None and all(c in db.columns for c in ["__K_VIA", "__K_COMUNE"]):
            mask = db["__K_VIA"].eq(via) & db["__K_COMUNE"].eq(comune)
            candidati = db.loc[mask, "ZONA"].dropna()
            if len(candidati) == 1:
                valore_trovato = candidati.iloc[0]
            elif len(candidati) > 1:
                # Se ci sono piu' clienti allo stesso indirizzo, scegliamo la
                # prima ZONA disponibile invece di perdere completamente il gruppo.
                valore_trovato = candidati.iloc[0]

        risultati.append(_gruppo_da_zona(valore_trovato))

    return risultati

def _calcola_penalita_gruppo(distanze):
    """Penalita' dinamica per preferire blocchi ZONA senza renderli rigidi.

    La penalita' e' espressa nella stessa unita' del costo OR-Tools (metri +
    secondi*10) e viene dimensionata sulla distanza media delle tratte reali.
    """
    valori = []
    for riga in distanze:
        for d in riga:
            if d is not None and float(d) > 0:
                valori.append(float(d))
    if not valori:
        return 0.0
    valori.sort()
    mediana = valori[len(valori) // 2]
    # Forte preferenza per non uscire/rientrare continuamente nei gruppi,
    # ma non un vincolo assoluto: una strada molto migliore puo' vincere.
    return max(5000.0, mediana * 2.5)


def _costo_arco_gruppi(a, b, distanze, durate, gruppi, penalita_gruppo):
    d = distanze[a][b]
    t = durate[a][b]
    if d is None or t is None:
        return 10**12
    costo = float(d) + float(t) * 10.0
    # Il deposito (0) non appartiene a nessun gruppo. La penalita' viene
    # applicata solo quando si passa direttamente da un gruppo a un altro.
    ga = gruppi[a] if a < len(gruppi) else None
    gb = gruppi[b] if b < len(gruppi) else None
    if ga is not None and gb is not None and ga != gb:
        costo += penalita_gruppo
    return int(round(costo))


def _ottimizza_con_ortools(distanze, durate, n_clienti, gruppi=None, penalita_gruppo=0.0):
    """Ottimizzazione locale: un solo furgone, deposito fisso, gruppi ZONA preferiti."""
    try:
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    except ImportError:
        return None, "OR-Tools non installato"

    # Indice 0 = deposito; 1..n = clienti.
    manager = pywrapcp.RoutingIndexManager(n_clienti + 1, 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def costo_arco(from_index, to_index):
        a = manager.IndexToNode(from_index)
        b = manager.IndexToNode(to_index)
        return _costo_arco_gruppi(a, b, distanze, durate, gruppi or [None] * (n_clienti + 1), penalita_gruppo)

    transit_callback = routing.RegisterTransitCallback(costo_arco)
    routing.SetArcCostEvaluatorOfAllVehicles(transit_callback)

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.seconds = 8

    soluzione = routing.SolveWithParameters(search_parameters)
    if soluzione is None:
        return None, "OR-Tools non ha trovato una soluzione"

    ordine = []
    index = routing.Start(0)
    while not routing.IsEnd(index):
        ordine.append(manager.IndexToNode(index))
        index = soluzione.Value(routing.NextVar(index))
    ordine.append(manager.IndexToNode(index))
    return ordine, None


def _ottimizza_fallback(distanze, durate, n_clienti, gruppi=None, penalita_gruppo=0.0):
    """Fallback senza OR-Tools: nearest-neighbour + 2-opt con preferenza ZONA."""
    non_visitati = set(range(1, n_clienti + 1))
    ordine = [0]
    while non_visitati:
        corrente = ordine[-1]
        prossimo = min(
            non_visitati,
            key=lambda j: _costo_arco_gruppi(
                corrente, j, distanze, durate, gruppi or [None] * (n_clienti + 1), penalita_gruppo
            )
        )
        ordine.append(prossimo)
        non_visitati.remove(prossimo)
    ordine.append(0)

    def costo(seq):
        totale = 0.0
        for a, b in zip(seq[:-1], seq[1:]):
            if distanze[a][b] is None or durate[a][b] is None:
                return float("inf")
            totale += _costo_arco_gruppi(
                a, b, distanze, durate, gruppi or [None] * (n_clienti + 1), penalita_gruppo
            )
        return totale

    migliorato = True
    while migliorato:
        migliorato = False
        migliore_costo = costo(ordine)
        # Il deposito resta fisso alle estremita'.
        for i in range(1, len(ordine) - 2):
            for j in range(i + 1, len(ordine) - 1):
                candidato = ordine[:i] + ordine[i:j + 1][::-1] + ordine[j + 1:]
                costo_candidato = costo(candidato)
                if costo_candidato + 0.01 < migliore_costo:
                    ordine = candidato
                    migliore_costo = costo_candidato
                    migliorato = True
        
    return ordine



def _metriche_gruppamento_ordine(ordine, gruppi):
    """Restituisce cambi ZONA e rientri in una ZONA già abbandonata."""
    seq = []
    for idx in ordine:
        if idx == 0:
            continue
        g = gruppi[idx] if idx < len(gruppi) else None
        if g is not None:
            seq.append(g)
    cambi = 0
    rientri = 0
    viste = set()
    precedente = None
    for g in seq:
        if precedente is not None and g != precedente:
            cambi += 1
            if g in viste:
                rientri += 1
        viste.add(g)
        precedente = g
    return cambi, rientri, seq


def _costo_base_ordine(ordine, distanze, durate):
    totale = 0.0
    for a, b in zip(ordine[:-1], ordine[1:]):
        d = distanze[a][b]
        t = durate[a][b]
        if d is None or t is None:
            return float("inf")
        totale += float(d) + float(t) * 10.0
    return totale


def _costo_arco_base(a, b, distanze, durate):
    d = distanze[a][b]
    t = durate[a][b]
    if d is None or t is None:
        return 10**15
    return float(d) + float(t) * 10.0


def _ordine_blocchi_da_sequenza_gruppi(distanze, durate, gruppi, sequenza_gruppi):
    """Costruisce un percorso in cui ogni macro-ZONA compare in un unico blocco."""
    ordine = [0]
    corrente = 0
    membri = {}
    for i in range(1, len(gruppi)):
        g = gruppi[i]
        if g is not None:
            membri.setdefault(g, []).append(i)

    for g in sequenza_gruppi:
        da_visitare = set(membri.get(g, []))
        while da_visitare:
            prossimo = min(
                da_visitare,
                key=lambda j: _costo_arco_base(corrente, j, distanze, durate)
            )
            ordine.append(prossimo)
            corrente = prossimo
            da_visitare.remove(prossimo)

        # Migliora l'ordine interno del blocco senza permettere che la ZONA
        # venga interrotta. Piccolo 2-opt locale sul solo blocco appena creato.
        pos_inizio = 1
        for k in range(1, len(ordine)):
            if gruppi[ordine[k]] == g:
                pos_inizio = k
            else:
                break
        pos_fine = len(ordine) - 1
        while pos_fine >= pos_inizio and gruppi[ordine[pos_fine]] != g:
            pos_fine -= 1
        if pos_fine - pos_inizio >= 2:
            migliorato = True
            while migliorato:
                migliorato = False
                migliore = sum(_costo_arco_base(a, b, distanze, durate)
                               for a, b in zip(ordine[pos_inizio-1:pos_fine+1], ordine[pos_inizio:pos_fine+2]))
                for i in range(pos_inizio, pos_fine):
                    for j in range(i+1, pos_fine+1):
                        candidato = ordine[:i] + ordine[i:j+1][::-1] + ordine[j+1:]
                        costo = sum(_costo_arco_base(a, b, distanze, durate)
                                    for a, b in zip(candidato[pos_inizio-1:pos_fine+1], candidato[pos_inizio:pos_fine+2]))
                        if costo + 0.01 < migliore:
                            ordine = candidato
                            migliore = costo
                            migliorato = True
                            break
                    if migliorato:
                        break

    # Fermate senza ZONA alla fine, senza alterare il raggruppamento delle altre.
    senza = [i for i in range(1, len(gruppi)) if gruppi[i] is None]
    while senza:
        prossimo = min(senza, key=lambda j: _costo_arco_base(corrente, j, distanze, durate))
        ordine.append(prossimo)
        corrente = prossimo
        senza.remove(prossimo)
    ordine.append(0)
    return ordine


def _ottimizza_a_blocchi_zona(distanze, durate, n_clienti, gruppi, forza_gruppamento_zona=100):
    """Ottimizzazione a DUE LIVELLI.

    Livello 1: decide l'ordine delle macro-ZONE.
    Livello 2: dentro ogni macro-ZONA ottimizza le fermate sulla strada.

    La ZONA non puo' essere spezzata: una volta terminato un blocco non si
    torna piu' a quel blocco. Questo e' il comportamento richiesto al 100%.
    """
    from itertools import permutations

    gruppi_validi = sorted({gruppi[i] for i in range(1, n_clienti + 1)
                             if i < len(gruppi) and gruppi[i] is not None})
    if not gruppi_validi:
        return [0] + list(range(1, n_clienti + 1)) + [0]

    membri = {
        g: [i for i in range(1, n_clienti + 1)
            if i < len(gruppi) and gruppi[i] == g]
        for g in gruppi_validi
    }

    def costo(a, b):
        return _costo_arco_base(a, b, distanze, durate)

    def ottimizza_blocco(membri_blocco, ingresso):
        """Trova un buon ordine stradale per un singolo blocco ZONA."""
        if len(membri_blocco) <= 1:
            return list(membri_blocco)

        non_visitati = set(membri_blocco)
        ordine = []
        corrente = ingresso
        while non_visitati:
            prossimo = min(non_visitati, key=lambda j: costo(corrente, j))
            ordine.append(prossimo)
            non_visitati.remove(prossimo)
            corrente = prossimo

        # 2-opt solo dentro il blocco: non puo' spostare una fermata fuori ZONA.
        migliorato = True
        while migliorato and len(ordine) >= 3:
            migliorato = False
            migliore = costo(ingresso, ordine[0])
            migliore += sum(costo(a, b) for a, b in zip(ordine[:-1], ordine[1:]))

            for i in range(len(ordine) - 1):
                for j in range(i + 1, len(ordine)):
                    cand = ordine[:i] + ordine[i:j + 1][::-1] + ordine[j + 1:]
                    val = costo(ingresso, cand[0])
                    val += sum(costo(a, b) for a, b in zip(cand[:-1], cand[1:]))
                    if val + 0.01 < migliore:
                        ordine = cand
                        migliore = val
                        migliorato = True
                        break
                if migliorato:
                    break
        return ordine

    def costruisci(seq_zone):
        ordine = [0]
        corrente = 0
        for g in seq_zone:
            blocco = ottimizza_blocco(membri[g], corrente)
            ordine.extend(blocco)
            if blocco:
                corrente = blocco[-1]
        ordine.append(0)
        return ordine

    # ORDINE PREFERITO DELLE MACRO-ZONE: numerico crescente.
    # Esempio: 1 -> 2 -> 3 -> 4 -> 5 -> 6 -> 7.
    # A 100% questo ordine diventa la priorita' assoluta per i blocchi;
    # l'ottimizzazione stradale continua invece a lavorare dentro ogni blocco.
    forza = max(0, min(100, int(forza_gruppamento_zona)))
    sequenza_crescente = sorted(gruppi_validi)

    if forza >= 100:
        # MODALITA' 100% RICHIESTA:
        # 1) ordine obbligatorio delle macro-ZONE: 1 -> 2 -> 3 -> ...
        # 2) ogni ZONA viene completata prima di passare alla successiva
        # 3) dentro ogni ZONA ottimizziamo la sequenza stradale dei clienti
        return costruisci(sequenza_crescente)

    # Con pochi gruppi possiamo provare TUTTI gli ordini possibili e scegliere
    # un compromesso reale tra strada e ordine crescente delle ZONE.
    if len(gruppi_validi) <= 8:
        sequenze = permutations(gruppi_validi)
        candidati = []
        posizione_ideale = {g: i for i, g in enumerate(sequenza_crescente)}
        for seq in sequenze:
            ordine = costruisci(seq)
            costo_strada = _costo_base_ordine(ordine, distanze, durate)
            # Distanza dall'ordine numerico ideale: piu' bassa = piu' simile
            # a 1 -> 2 -> 3 -> ...
            costo_ordine = sum(abs(i - posizione_ideale[g]) for i, g in enumerate(seq))
            candidati.append((costo_strada, costo_ordine, ordine))

        min_strada = min(x[0] for x in candidati)
        max_strada = max(x[0] for x in candidati)
        min_ordine = min(x[1] for x in candidati)
        max_ordine = max(x[1] for x in candidati)

        def normalizza(x, minimo, massimo):
            return 0.0 if massimo - minimo <= 1e-9 else (x - minimo) / (massimo - minimo)

        f = forza / 100.0
        migliore = min(
            candidati,
            key=lambda x: (
                (1.0 - f) * normalizza(x[0], min_strada, max_strada)
                + f * normalizza(x[1], min_ordine, max_ordine),
                x[0]
            )
        )
        return migliore[2]

    # Oltre 8 gruppi, il fattoriale cresce troppo: usiamo piu' strategie
    # deterministiche, mantenendo il vincolo di blocco e includendo sempre
    # l'ordine numerico crescente come candidato.
    sequenze = []
    crescente = list(gruppi_validi)
    decrescente = list(reversed(crescente))
    per_deposito = sorted(
        gruppi_validi,
        key=lambda g: min(costo(0, j) for j in membri[g])
    )
    sequenze.extend([crescente, decrescente, per_deposito, list(reversed(per_deposito))])

    # Greedy tra blocchi usando la miglior uscita del blocco corrente.
    for prima in gruppi_validi:
        rimanenti = set(gruppi_validi)
        rimanenti.remove(prima)
        seq = [prima]
        corrente = min(membri[prima], key=lambda j: costo(0, j))
        while rimanenti:
            g = min(
                rimanenti,
                key=lambda z: min(costo(corrente, j) for j in membri[z])
            )
            seq.append(g)
            corrente = min(membri[g], key=lambda j: costo(corrente, j))
            rimanenti.remove(g)
        sequenze.append(seq)

    candidati = []
    viste = set()
    posizione_ideale = {g: i for i, g in enumerate(crescente)}
    for seq in sequenze:
        chiave = tuple(seq)
        if chiave in viste:
            continue
        viste.add(chiave)
        ordine = costruisci(seq)
        costo_strada = _costo_base_ordine(ordine, distanze, durate)
        costo_ordine = sum(abs(i - posizione_ideale[g]) for i, g in enumerate(seq))
        candidati.append((costo_strada, costo_ordine, ordine))

    min_strada = min(x[0] for x in candidati)
    max_strada = max(x[0] for x in candidati)
    min_ordine = min(x[1] for x in candidati)
    max_ordine = max(x[1] for x in candidati)
    f = forza / 100.0

    def normalizza(x, minimo, massimo):
        return 0.0 if massimo - minimo <= 1e-9 else (x - minimo) / (massimo - minimo)

    return min(
        candidati,
        key=lambda x: (
            (1.0 - f) * normalizza(x[0], min_strada, max_strada)
            + f * normalizza(x[1], min_ordine, max_ordine),
            x[0]
        )
    )[2]

def ottimizza_giro_free(df_giro, df_db=None, forza_gruppamento_zona=75):
    """Ottimizza il giro su strada con una seconda priorita' REALE per ZONA.

    0%  = solo strada.
    100% = modalita' STRICT ZONA: prima ZONA 1, poi ZONA 2, poi ZONA 3...;
           ogni ZONA viene completata prima di passare alla successiva e
           l'ordine dei clienti viene ottimizzato SOLO all'interno della ZONA.
    0% = solo strada, ZONA completamente ignorata.
    Valori intermedi = compromesso: le ZONE possono essere mischiate in base
           al criterio stradale, come richiesto dall'utente.
    ORA non viene mai usata.
    """
    if df_giro is None or df_giro.empty:
        raise ValueError("Il giro è vuoto.")
    if len(df_giro) > 99:
        raise ValueError("Il giro contiene più di 99 fermate: il servizio OSRM pubblico non è adatto a questo volume in una singola matrice.")

    df_originale = df_giro.copy().reset_index(drop=True)

    # Recuperiamo le ZONE prima della geocodifica: se il database e' corretto,
    # ogni fermata deve poter essere associata a una macro-ZONA.
    gruppi_clienti = _gruppi_fermate(df_originale, df_db)
    gruppi_presenti_pre = sorted({g for g in gruppi_clienti if g is not None})

    coordinate = [COORDINATE_DEPOSITO_VANGO]
    indirizzi_non_trovati = []
    coordinate_da_salvare = {}

    for idx, (_, row) in enumerate(df_originale.iterrows(), start=1):
        indirizzo = _indirizzo_riga(row)
        if not indirizzo.strip():
            indirizzi_non_trovati.append(f"Fermata {idx}: indirizzo vuoto")
            continue
        coord = _trova_coordinate_nel_db(row, df_db)
        if coord is None:
            risultato = _geocodifica_free(indirizzo)
            if risultato is not None:
                coord = (risultato["lat"], risultato["lon"])
                cliente_key = (
                    str(row.get("CLIENTE", "")).strip().casefold(),
                    str(row.get("VIA", "")).strip().casefold(),
                    str(row.get("COMUNE", "")).strip().casefold(),
                )
                coordinate_da_salvare[cliente_key] = coord
        if coord is None:
            indirizzi_non_trovati.append(indirizzo)
        else:
            coordinate.append(coord)

    if indirizzi_non_trovati:
        elenco = "\n".join(f"- {x}" for x in indirizzi_non_trovati[:8])
        if len(indirizzi_non_trovati) > 8:
            elenco += f"\n- ... e altre {len(indirizzi_non_trovati) - 8}"
        raise ValueError("Non riesco a geolocalizzare alcuni indirizzi con OpenStreetMap:\n" + elenco)

    distanze, durate = _richiedi_matrice_osrm(coordinate)
    forza_gruppamento_zona = max(0, min(100, int(forza_gruppamento_zona)))

    # ZONA e' stata recuperata in modo robusto prima della matrice OSRM.
    gruppi = [None] + gruppi_clienti
    gruppi_presenti = gruppi_presenti_pre
    penalita_base = _calcola_penalita_gruppo(distanze)
    penalita_gruppo = penalita_base * (forza_gruppamento_zona / 100.0)

    ordine_originale = [0] + list(range(1, len(df_originale) + 1)) + [0]
    km_originali, minuti_originali = _percorso_da_indici(ordine_originale, distanze, durate)

    # Candidato A: migliore percorso stradale puro.
    candidati = []
    ordine_puro, errore_ortools = _ottimizza_con_ortools(
        distanze, durate, len(df_originale),
        gruppi=[None] * (len(df_originale) + 1), penalita_gruppo=0.0
    )
    if ordine_puro is not None:
        candidati.append(("STRADA", ordine_puro))
    else:
        ordine_puro = _ottimizza_fallback(
            distanze, durate, len(df_originale),
            gruppi=[None] * (len(df_originale) + 1), penalita_gruppo=0.0
        )
        candidati.append(("STRADA fallback", ordine_puro))

    # Candidato B: percorso realmente costruito per blocchi ZONA.
    # Questo e' il candidato che al 100% deve vincere se esistono piu' gruppi.
    ordine_blocchi = None
    if len(gruppi_presenti) >= 2:
        ordine_blocchi = _ottimizza_a_blocchi_zona(
            distanze, durate, len(df_originale), gruppi, forza_gruppamento_zona
        )
        candidati.append(("BLOCCHI ZONA", ordine_blocchi))

    # Candidato C: OR-Tools con forte penalita' sui cambi ZONA, utile come
    # compromesso nei valori intermedi.
    if len(gruppi_presenti) >= 2 and forza_gruppamento_zona > 0:
        ordine_pen, _ = _ottimizza_con_ortools(
            distanze, durate, len(df_originale),
            gruppi=gruppi,
            penalita_gruppo=penalita_base * (forza_gruppamento_zona / 100.0) * 8.0
        )
        if ordine_pen is not None:
            candidati.append(("STRADA + ZONA", ordine_pen))

    dettagli = {}
    for nome, ordine in candidati:
        base = _costo_base_ordine(ordine, distanze, durate)
        cambi, rientri, seq = _metriche_gruppamento_ordine(ordine, gruppi)
        # Penalizziamo molto il rientro in un gruppo gia' chiuso: e' proprio
        # il comportamento che vogliamo evitare quando la forza aumenta.
        costo_zona = float(cambi) + float(rientri) * 5.0
        dettagli[nome] = {
            "base": base,
            "zona": costo_zona,
            "cambi": cambi,
            "rientri": rientri,
            "seq": seq,
        }

    if len(gruppi_presenti) < 2:
        nome_scelto, ordine_ottimizzato = candidati[0]
    elif forza_gruppamento_zona >= 100:
        # A 100% la ZONA e' una priorita' rigida sull'ordine dei blocchi:
        # NON si confronta con il percorso stradale puro.
        nome_scelto, ordine_ottimizzato = ("BLOCCHI ZONA CRESCENTI (2 LIVELLI)", ordine_blocchi)
    elif forza_gruppamento_zona <= 5:
        nome_scelto, ordine_ottimizzato = candidati[0]
    else:
        # Tra 5 e 95% scegliamo il compromesso. Il costo stradale e quello
        # ZONA sono normalizzati tra i candidati, quindi la percentuale ha un
        # significato diretto e non dipende da una penalita' arbitraria.
        basi = [v["base"] for v in dettagli.values()]
        zone = [v["zona"] for v in dettagli.values()]
        min_b, max_b = min(basi), max(basi)
        min_z, max_z = min(zone), max(zone)

        def norm(x, a, b):
            return 0.0 if b - a <= 1e-9 else (x - a) / (b - a)

        f = forza_gruppamento_zona / 100.0
        def score(item):
            nome, _ = item
            d = dettagli[nome]
            return (1-f) * norm(d["base"], min_b, max_b) + f * norm(d["zona"], min_z, max_z)

        nome_scelto, ordine_ottimizzato = min(candidati, key=lambda x: (score(x), dettagli[x[0]]["base"]))

    km_ottimizzati, secondi_ottimizzati = _percorso_da_indici(ordine_ottimizzato, distanze, durate)
    cambi_zona, rientri_zona, sequenza_zona = _metriche_gruppamento_ordine(ordine_ottimizzato, gruppi)
    indici_clienti = [i - 1 for i in ordine_ottimizzato if i != 0]
    df_ottimizzato = df_originale.iloc[indici_clienti].reset_index(drop=True).copy()
    df_ottimizzato["POSIZIONE"] = [str(i) for i in range(1, len(df_ottimizzato) + 1)]

    metriche = {
        "metodo": nome_scelto,
        "fermate": len(df_originale),
        "km_originali": km_originali / 1000.0,
        "min_originali": minuti_originali / 60.0,
        "km_ottimizzati": km_ottimizzati / 1000.0,
        "min_ottimizzati": secondi_ottimizzati / 60.0,
        "risparmio_km": (km_originali - km_ottimizzati) / 1000.0,
        "risparmio_min": (minuti_originali - secondi_ottimizzati) / 60.0,
        "errore_ortools": errore_ortools,
        "gruppi_zona": len(gruppi_presenti),
        "penalita_gruppo": penalita_gruppo,
        "forza_gruppamento_zona": forza_gruppamento_zona,
        "cambi_zona": cambi_zona,
        "rientri_zona": rientri_zona,
        "sequenza_zona": sequenza_zona,
        "coordinate_da_salvare": coordinate_da_salvare,
        "debug_gruppamento": {
            "gruppi_presenti": gruppi_presenti,
            "cambi_zona": cambi_zona,
            "rientri_zona": rientri_zona,
            "candidati": {
                k: {"costo_strada": round(v["base"], 1), "costo_zona": v["zona"], "cambi": v["cambi"], "rientri": v["rientri"]}
                for k, v in dettagli.items()
            },
        },
    }
    return df_ottimizzato, metriche



def _parse_orario_apertura(valore):
    """Interpreta ORA come apertura minima.

    Regola V10 TEST:
    - 01:00 = ORARIO SCONOSCIUTO -> nessun vincolo temporale.
    - vuoto/non interpretabile = nessun vincolo temporale.
    - HH:MM = cliente disponibile da quell'ora in poi.
    """
    if valore is None:
        return None
    try:
        if pd.isna(valore):
            return None
    except Exception:
        pass

    testo = str(valore).strip()
    if not testo or testo.lower() in ("nan", "nat", "none", "null"):
        return None

    # 01:00 e' il nostro codice per "orario sconosciuto".
    if testo.startswith("01:00") or testo in ("1:00", "1:0", "01:0"):
        return None

    import re
    match = re.search(r"(?<!\d)(\d{1,2}):(\d{2})(?::\d{2})?", testo)
    if not match:
        return None

    ore = int(match.group(1))
    minuti = int(match.group(2))
    if ore < 0 or ore > 23 or minuti < 0 or minuti > 59:
        return None
    return ore * 60 + minuti


def _formatta_ora_minuti(minuti):
    """Formatta minuti dalla mezzanotte in HH:MM."""
    minuti = int(max(0, minuti))
    ore = (minuti // 60) % 24
    mins = minuti % 60
    return f"{ore:02d}:{mins:02d}"


def _intero_sicuro(valore, default=0):
    """Converte un valore numerico in intero evitando errori con NaN/valori vuoti."""
    try:
        numero = pd.to_numeric(valore, errors="coerce")
        if pd.isna(numero):
            return int(default)
        return int(round(float(numero)))
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _formatta_durata_hm(minuti):
    """Formatta una durata in minuti come 'Xh YYm' (es. 1h 43m), o 'YYm' se sotto l'ora.

    Usata ovunque nell'app per mostrare durate in modo uniforme (Attesa totale,
    Servizio totale, Tempo totale reale giro, Tempo Giro, ecc.), cosi' non
    compaiono piu' numeri di minuti "grezzi" tipo "252 min".
    """
    minuti = max(0, int(round(float(minuti or 0))))
    ore, minuti_restanti = divmod(minuti, 60)
    return f"{ore}h {minuti_restanti:02d}m" if ore > 0 else f"{minuti_restanti}m"


def _ora_partenza_reale_minuti():
    """Restituisce l'ora reale di partenza SOLO dopo INIZIA GIRO.

    Non esiste piu' alcun orario di partenza implicito o fallback automatico.
    Se INIZIA GIRO non e' stato premuto, restituisce None.
    """
    timestamp = st.session_state.get("inizio_giro_reale")
    if timestamp is None:
        return None
    try:
        tz = ZoneInfo("Europe/Rome") if ZoneInfo is not None else None
        dt = datetime.fromtimestamp(float(timestamp), tz) if tz else datetime.fromtimestamp(float(timestamp))
        return dt.hour * 60 + dt.minute + dt.second / 60.0
    except Exception:
        return None


def _formatta_ora_partenza_reale():
    minuti = _ora_partenza_reale_minuti()
    if minuti is None:
        return "—"
    return _formatta_ora_minuti(round(minuti))


def _assicura_colonne_colli(df):
    """Garantisce le colonne operative dei colli senza alterare Q.ta."""
    out = df.copy()
    for c in ["COLLI_CONSEGNATI", "COLLI_RIFIUTATI", "COLLI_DA_RENDERE"]:
        if c not in out.columns:
            out[c] = 0.0
        out[c] = pd.to_numeric(out[c], errors="coerce").fillna(0.0).astype(float)
    if "Q.ta" not in out.columns:
        out["Q.ta"] = 0.0
    out["Q.ta"] = pd.to_numeric(out["Q.ta"], errors="coerce").fillna(0.0)
    return out

def _totali_colli_giro(df):
    """Restituisce iniziali, residui, consegnati e rifiutati del giro."""
    if df is None or df.empty:
        return {"iniziali": 0, "residui": 0, "consegnati": 0, "rifiutati": 0, "da_rendere": 0}
    x = _assicura_colonne_colli(df)
    gestiti = x["STATO"].fillna("").astype(str).isin([STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO]) if "STATO" in x.columns else pd.Series(False, index=x.index)
    iniziali = int(round(float(x["Q.ta"].sum())))
    residui = int(round(float(x.loc[~gestiti, "Q.ta"].sum())))
    consegnati = int(round(float(x["COLLI_CONSEGNATI"].sum())))
    rifiutati = int(round(float(x["COLLI_RIFIUTATI"].sum())))
    da_rendere = int(round(float(x["COLLI_DA_RENDERE"].sum())))
    return {"iniziali": iniziali, "residui": residui, "consegnati": consegnati, "rifiutati": rifiutati, "da_rendere": da_rendere}

def _minuti_fermo_totali():
    totale = float(st.session_state.get("minuti_fermo_mezzo", 0) or 0)
    if st.session_state.get("fermo_mezzo_attivo") and st.session_state.get("inizio_fermo_mezzo"):
        try:
            totale += max(0.0, (time.time() - float(st.session_state.inizio_fermo_mezzo)) / 60.0)
        except Exception:
            pass
    return totale

def _minuti_trascorsi_da_inizio_giro():
    """Minuti reali trascorsi SOLO da INIZIA GIRO, escluse le pause."""
    timestamp = st.session_state.get("inizio_giro_reale")
    if timestamp is None:
        return 0.0
    try:
        trascorsi = max(0.0, (time.time() - float(timestamp)) / 60.0)
    except Exception:
        return 0.0
    return max(0.0, trascorsi - _minuti_fermo_totali())


def _stato_avanzamento_giro(fermate_completate, fermate_totali, previsto_totale_min):
    """Confronta ETA prevista e ETA live della prossima fermata.

    V2 ETA OSRM: l'origine e' l'ultima consegna gestita (FATTO/PARZIALE/
    RESPINTO); OSRM calcola la tratta stradale fino alla prima fermata ancora
    da consegnare nell'ordine corrente. Il GPS non viene usato per questo
    calcolo. L'ETA live parte dall'ora attuale e viene corretta per l'eventuale
    attesa dovuta all'orario di apertura della prossima fermata.

    Il giro non viene mai riordinato e non viene riottimizzato.
    """
    if not fermate_totali or not fermate_completate:
        return None

    df = st.session_state.get("giro_corrente")
    if df is None or df.empty:
        return None

    stati_gestiti = [STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO]
    stati = df.get(
        "STATO", pd.Series([STATO_DA_FARE] * len(df), index=df.index)
    ).fillna("").astype(str).str.strip()
    gestiti = df[stati.isin(stati_gestiti)]
    pendenti = df[~stati.isin(stati_gestiti)]
    if gestiti.empty or pendenti.empty:
        return None

    ultima_gestita = gestiti.iloc[-1]
    prossima = pendenti.iloc[0]

    # Coordinate: ultima consegna -> prossima consegna. Mai GPS.
    df_db = st.session_state.get("db_clienti")
    origine = _trova_coordinate_nel_db(ultima_gestita, df_db)
    destinazione = _trova_coordinate_nel_db(prossima, df_db)
    if origine is None or destinazione is None:
        return None

    # Il valore previsto della prossima fermata e' il cumulativo gia'
    # calcolato sul giro. E' un tempo relativo all'inizio del giro.
    previsto_arrivo = _numero_minuti_cumulativi(
        prossima.get("MIN_PREVISTI_CUMULATIVI")
    )
    if previsto_arrivo is None:
        return None

    try:
        tz = ZoneInfo("Europe/Rome") if ZoneInfo is not None else None
        adesso = datetime.now(tz) if tz else datetime.now()
        ora_attuale = (
            adesso.hour * 60 + adesso.minute + adesso.second / 60.0
        )
    except Exception:
        ora_attuale = _ora_partenza_reale_minuti()

    # Cache breve: evita di interrogare il server OSRM piu' volte durante
    # lo stesso minuto/rerun, ma permette di aggiornare la stima durante il giro.
    cache = st.session_state.get("eta_osrm_prossima") or {}
    firma = (
        str(ultima_gestita.get("CLIENTE", "")),
        str(prossima.get("CLIENTE", "")),
        str(ultima_gestita.get("VIA", "")),
        str(prossima.get("VIA", "")),
    )
    adesso_ts = time.time()
    durata_strada_min = None
    if cache.get("firma") == firma and adesso_ts - float(cache.get("timestamp", 0) or 0) < 60:
        try:
            durata_strada_min = float(cache.get("minuti_strada"))
        except Exception:
            durata_strada_min = None

    if durata_strada_min is None:
        try:
            distanze, durate = _richiedi_matrice_osrm([origine, destinazione])
            durata = durate[0][1]
            if durata is None:
                return None
            durata_strada_min = max(0.0, float(durata) / 60.0)
            st.session_state.eta_osrm_prossima = {
                "firma": firma,
                "timestamp": adesso_ts,
                "minuti_strada": durata_strada_min,
            }
        except Exception:
            return None

    eta_live = ora_attuale + durata_strada_min

    # Se la prossima consegna ha un orario di apertura noto, l'ETA non puo'
    # essere precedente all'apertura. Il valore 01:00 resta sconosciuto.
    apertura = _parse_orario_apertura(prossima.get("ORA", ""))
    if apertura is not None:
        eta_live = max(eta_live, float(apertura))

    # L'ETA prevista assoluta nasce dall'orario di partenza previsto/reale
    # usato per costruire i cumulativi.
    ora_partenza = _ora_partenza_reale_minuti()
    previsto_assoluto = float(ora_partenza) + float(previsto_arrivo)

    scarto = previsto_assoluto - eta_live
    SOGLIA_IN_LINEA_MIN = 5

    previsto_txt = _formatta_ora_minuti(round(previsto_assoluto))
    stimato_txt = _formatta_ora_minuti(round(eta_live))
    dettaglio_base = f"Prossima: {prossima.get('CLIENTE', 'cliente')} · previsto {previsto_txt} · stimato {stimato_txt}"

    if abs(scarto) < SOGLIA_IN_LINEA_MIN:
        return {
            "emoji": "🟡",
            "colore": "#F59E0B",
            "testo": "In linea con la previsione",
            "dettaglio": f"{dettaglio_base} · scarto di {_formatta_durata_hm(abs(scarto))}",
        }
    if scarto > 0:
        return {
            "emoji": "🟢",
            "colore": "#22C55E",
            "testo": f"In anticipo di {_formatta_durata_hm(scarto)}",
            "dettaglio": f"{dettaglio_base} · rispetto all'arrivo previsto",
        }
    return {
        "emoji": "🔴",
        "colore": "#EF4444",
        "testo": f"In ritardo di {_formatta_durata_hm(abs(scarto))}",
        "dettaglio": f"{dettaglio_base} · rispetto all'arrivo previsto",
    }


def _simula_tempo_percorso_orari(ordine, durate, orari_apertura, ora_partenza_minuti=300, minuti_servizio=MINUTI_SERVIZIO_PER_FERMATA):
    """Simula l'orario reale fermata per fermata.

    Regola: si viaggia, si arriva, si attende solo se necessario per l'apertura,
    poi si effettuano 12 minuti di parcheggio+scarico prima di ripartire.
    Il servizio viene applicato a ogni cliente, ma non al deposito finale.
    """
    tempo = float(ora_partenza_minuti)
    arrivi = {}
    attese = {}
    servizi = {}
    for a, b in zip(ordine[:-1], ordine[1:]):
        viaggio = durate[a][b]
        if viaggio is None:
            return None
        tempo += float(viaggio) / 60.0
        if b != 0:
            apertura = orari_apertura[b - 1] if b - 1 < len(orari_apertura) else None
            attesa = max(0.0, float(apertura) - tempo) if apertura is not None else 0.0
            tempo += attesa
            arrivi[b] = tempo
            attese[b] = attesa
            tempo += float(minuti_servizio)
            servizi[b] = float(minuti_servizio)
    return {"arrivi": arrivi, "attese": attese, "servizi": servizi, "fine": tempo}


def _ottimizza_con_ortools_orari(distanze, durate, df_giro, ora_partenza_minuti=300):
    """V10.2 TEST: un solo furgone + aperture + 12 min medi per fermata.

    OSRM fornisce i tempi stradali; OR-Tools decide l'ordine.
    Non esistono orari di chiusura nel DB, quindi ogni ORA valida e' trattata
    come "non prima di HH:MM". 01:00/blank = nessun vincolo.
    """
    try:
        from ortools.constraint_solver import pywrapcp, routing_enums_pb2
    except ImportError:
        return None, "OR-Tools non installato", None

    # OR-Tools richiede interi puri (int64_t) per slack/capacity/SetRange.
    # _ora_partenza_reale_minuti() puo' restituire un float (include i secondi),
    # quindi va arrotondato subito per evitare errori di tipo nel binding SWIG.
    ora_partenza_minuti = int(round(float(ora_partenza_minuti)))

    n_clienti = len(df_giro)
    manager = pywrapcp.RoutingIndexManager(n_clienti + 1, 1, 0)
    routing = pywrapcp.RoutingModel(manager)

    def costo_arco(from_index, to_index):
        a = manager.IndexToNode(from_index)
        b = manager.IndexToNode(to_index)
        d = distanze[a][b]
        t = durate[a][b]
        if d is None or t is None:
            return 10**12
        # Manteniamo lo stesso criterio stradale del motore V9.
        return int(round(float(d) + float(t) * 10.0))

    costo_callback = routing.RegisterTransitCallback(costo_arco)
    routing.SetArcCostEvaluatorOfAllVehicles(costo_callback)

    # Ogni cliente richiede in media 12 minuti per parcheggio + scarico.
    # Il tempo di servizio viene aggiunto dopo l'arrivo al cliente e quindi
    # influisce sull'orario di arrivo di tutte le fermate successive.
    def tempo_arco(from_index, to_index):
        a = manager.IndexToNode(from_index)
        b = manager.IndexToNode(to_index)
        t = durate[a][b]
        if t is None:
            return 10**9
        # Il servizio della fermata di partenza viene conteggiato qui.
        # Usiamo minuti interi perché la dimensione Tempo di OR-Tools è in minuti.
        # Il calcolo dettagliato finale usa comunque i secondi OSRM.
        viaggio_min = max(0, int(round(float(t) / 60.0)))
        servizio_min = MINUTI_SERVIZIO_PER_FERMATA if a != 0 else 0
        return viaggio_min + servizio_min

    tempo_callback = routing.RegisterTransitCallback(tempo_arco)

    # Orizzonte: dalle 05:00 fino a fine giornata.  Il tempo e' espresso
    # come minuti trascorsi dall'inizio del giro alle 05:00.
    fine_giornata = 24 * 60
    slack_massimo = fine_giornata
    routing.AddDimension(
        tempo_callback,
        slack_massimo,
        fine_giornata - ora_partenza_minuti,
        True,
        "Tempo"
    )
    dimensione_tempo = routing.GetDimensionOrDie("Tempo")

    # Il deposito parte esattamente alle ora_partenza_minuti.
    dimensione_tempo.CumulVar(routing.Start(0)).SetValue(0)

    orari_apertura = []
    for _, row in df_giro.reset_index(drop=True).iterrows():
        orari_apertura.append(_parse_orario_apertura(row.get("ORA", "")))

    # Vincoli di apertura: nessun limite superiore, solo "non prima di".
    for i, apertura in enumerate(orari_apertura, start=1):
        if apertura is None:
            dimensione_tempo.CumulVar(manager.NodeToIndex(i)).SetRange(0, fine_giornata - ora_partenza_minuti)
        else:
            apertura_relativa = max(0, apertura - ora_partenza_minuti)
            if apertura_relativa > fine_giornata - ora_partenza_minuti:
                return None, f"L'orario { _formatta_ora_minuti(apertura) } supera l'orizzonte della giornata.", orari_apertura
            dimensione_tempo.CumulVar(manager.NodeToIndex(i)).SetRange(
                apertura_relativa,
                fine_giornata - ora_partenza_minuti
            )

    # IMPORTANTE: il costo stradale V9 usa:
    #   distanza (metri) + durata_stradale (secondi) * 10
    # 1 minuto di strada vale quindi circa 600 unita'.
    # L'attesa davanti a un cliente deve avere un peso reale nello stesso
    # ordine di grandezza, altrimenti OR-Tools la considera quasi gratis.
    # Con 600, 1 minuto di attesa pesa circa come 1 minuto di guida.
    COEFFICIENTE_ATTESA_MINUTO = 600
    try:
        dimensione_tempo.SetSlackCostCoefficientForAllVehicles(COEFFICIENTE_ATTESA_MINUTO)
        # Minimizza anche il tempo complessivo del giro, includendo viaggio,
        # attese e i 12 minuti medi di servizio per ogni cliente.
        dimensione_tempo.SetSpanCostCoefficientForAllVehicles(COEFFICIENTE_ATTESA_MINUTO)
    except Exception:
        pass

    search_parameters = pywrapcp.DefaultRoutingSearchParameters()
    search_parameters.first_solution_strategy = routing_enums_pb2.FirstSolutionStrategy.PATH_CHEAPEST_ARC
    search_parameters.local_search_metaheuristic = routing_enums_pb2.LocalSearchMetaheuristic.GUIDED_LOCAL_SEARCH
    search_parameters.time_limit.seconds = 12

    soluzione = routing.SolveWithParameters(search_parameters)
    if soluzione is None:
        return None, "OR-Tools non ha trovato una soluzione compatibile con gli orari.", orari_apertura

    ordine = []
    index = routing.Start(0)
    while not routing.IsEnd(index):
        ordine.append(manager.IndexToNode(index))
        index = soluzione.Value(routing.NextVar(index))
    ordine.append(manager.IndexToNode(index))

    arrivi_relativi = {}
    for node in ordine:
        if node == 0:
            continue
        index_node = manager.NodeToIndex(node)
        arrivi_relativi[node] = int(soluzione.Value(dimensione_tempo.CumulVar(index_node)))

    return ordine, None, {
        "orari_apertura": orari_apertura,
        "arrivi_relativi": arrivi_relativi,
        "ora_partenza_minuti": ora_partenza_minuti,
    }


def ottimizza_giro_orari_test(df_giro, df_db=None, ora_partenza_minuti=300):
    """V10.2 TEST ORARI: aperture + 12 min medi di servizio per fermata.

    E' una modalita' separata: non usa ZONA come criterio.
    01:00 e' sconosciuto e quindi non impone alcun vincolo temporale.
    Ogni cliente aggiunge 12 minuti di parcheggio + scarico al giro.
    """
    if df_giro is None or df_giro.empty:
        raise ValueError("Il giro è vuoto.")
    if len(df_giro) > 99:
        raise ValueError("Il giro contiene più di 99 fermate: il servizio OSRM pubblico non è adatto a questo volume in una singola matrice.")

    df_originale = df_giro.copy().reset_index(drop=True)
    coordinate = [COORDINATE_DEPOSITO_VANGO]
    indirizzi_non_trovati = []
    coordinate_da_salvare = {}

    for idx, (_, row) in enumerate(df_originale.iterrows(), start=1):
        indirizzo = _indirizzo_riga(row)
        if not indirizzo.strip():
            indirizzi_non_trovati.append(f"Fermata {idx}: indirizzo vuoto")
            continue
        coord = _trova_coordinate_nel_db(row, df_db)
        if coord is None:
            risultato = _geocodifica_free(indirizzo)
            if risultato is not None:
                coord = (risultato["lat"], risultato["lon"])
                cliente_key = (
                    str(row.get("CLIENTE", "")).strip().casefold(),
                    str(row.get("VIA", "")).strip().casefold(),
                    str(row.get("COMUNE", "")).strip().casefold(),
                )
                coordinate_da_salvare[cliente_key] = coord
        if coord is None:
            indirizzi_non_trovati.append(indirizzo)
        else:
            coordinate.append(coord)

    if indirizzi_non_trovati:
        elenco = "\n".join(f"- {x}" for x in indirizzi_non_trovati[:8])
        if len(indirizzi_non_trovati) > 8:
            elenco += f"\n- ... e altre {len(indirizzi_non_trovati) - 8}"
        raise ValueError("Non riesco a geolocalizzare alcuni indirizzi con OpenStreetMap:\n" + elenco)

    distanze, durate = _richiedi_matrice_osrm(coordinate)
    ordine_originale = [0] + list(range(1, len(df_originale) + 1)) + [0]
    km_originali, minuti_originali = _percorso_da_indici(ordine_originale, distanze, durate)

    ordine_ottimizzato, errore_ortools, dati_tempo = _ottimizza_con_ortools_orari(
        distanze, durate, df_originale, ora_partenza_minuti=ora_partenza_minuti
    )
    if ordine_ottimizzato is None:
        raise ValueError(errore_ortools or "Ottimizzazione ORARI non riuscita.")

    km_ottimizzati, secondi_ottimizzati = _percorso_da_indici(ordine_ottimizzato, distanze, durate)
    indici_clienti = [i - 1 for i in ordine_ottimizzato if i != 0]
    df_ottimizzato = df_originale.iloc[indici_clienti].reset_index(drop=True).copy()
    df_ottimizzato["POSIZIONE"] = [str(i) for i in range(1, len(df_ottimizzato) + 1)]

    # Simulazione finale con secondi OSRM: viaggio -> attesa -> 12 min servizio.
    orari_apertura = dati_tempo.get("orari_apertura", []) if isinstance(dati_tempo, dict) else []
    simulazione = _simula_tempo_percorso_orari(
        ordine_ottimizzato, durate, orari_apertura,
        ora_partenza_minuti=ora_partenza_minuti,
        minuti_servizio=MINUTI_SERVIZIO_PER_FERMATA,
    )
    if simulazione is None:
        raise ValueError("Impossibile simulare il tempo del percorso ORARI.")

    arrivi_assoluti = []
    attese = []
    for node in indici_clienti:
        arrivo_assoluto = simulazione["arrivi"].get(node + 1, float(ora_partenza_minuti))
        arrivi_assoluti.append(_formatta_ora_minuti(round(arrivo_assoluto)))
        attese.append(simulazione["attese"].get(node + 1, 0.0))

    df_ottimizzato["ARRIVO STIMATO"] = arrivi_assoluti

    orari_conosciuti = sum(1 for x in orari_apertura if x is not None)
    orari_sconosciuti = len(df_originale) - orari_conosciuti

    metriche = {
        "metodo": "ORARI — TEST + 12 MIN/FERMATA",
        "minuti_servizio_per_fermata": MINUTI_SERVIZIO_PER_FERMATA,
        "minuti_servizio_totali": len(df_originale) * MINUTI_SERVIZIO_PER_FERMATA,
        "fermate": len(df_originale),
        "km_originali": km_originali / 1000.0,
        "min_originali": minuti_originali / 60.0,
        "km_ottimizzati": km_ottimizzati / 1000.0,
        "min_ottimizzati": secondi_ottimizzati / 60.0,
        "risparmio_km": (km_originali - km_ottimizzati) / 1000.0,
        "risparmio_min": (minuti_originali - secondi_ottimizzati) / 60.0,
        "errore_ortools": errore_ortools,
        "orari_conosciuti": orari_conosciuti,
        "orari_sconosciuti": orari_sconosciuti,
        "ora_partenza": _formatta_ora_minuti(ora_partenza_minuti),
        "attesa_totale_min": round(sum(attese), 1),
        "servizio_totale_min": len(df_originale) * MINUTI_SERVIZIO_PER_FERMATA,
        "tempo_totale_reale_min": round(simulazione["fine"] - ora_partenza_minuti, 1),
        "coordinate_da_salvare": coordinate_da_salvare,
    }
    return df_ottimizzato, metriche

def geolocalizza_tutti_clienti(df_db, salvataggio_progressivo=None):
    """Geolocalizza i clienti senza coordinate e aggiorna la colonna H.

    IMPORTANTE: non usa st.cache_data per la geocodifica, perché anche un
    fallimento temporaneo verrebbe altrimenti memorizzato come None.
    Il salvataggio progressivo evita di perdere il lavoro già fatto.
    """
    if df_db is None or df_db.empty:
        return df_db.copy(), 0, 0, []

    risultato = df_db.copy()
    if "COORDINATE" not in risultato.columns:
        risultato["COORDINATE"] = ""

    trovati = 0
    gia_presenti = 0
    non_trovati = []
    totali = len(risultato)
    ultimo_salvataggio = 0

    progress = st.progress(0, text="🌍 Preparazione geolocalizzazione...")

    for posizione, (idx, row) in enumerate(risultato.iterrows(), start=1):
        esistente = _coordinate_riga_db(row)
        if esistente:
            gia_presenti += 1
        else:
            indirizzo = _indirizzo_riga(row)
            if not indirizzo.strip():
                non_trovati.append(f"{row.get('CLIENTE', 'Cliente')} — indirizzo vuoto")
            else:
                risultato_geo = _geocodifica_free(indirizzo)
                if risultato_geo is None:
                    non_trovati.append(f"{row.get('CLIENTE', 'Cliente')} — {indirizzo}")
                else:
                    risultato.at[idx, "COORDINATE"] = f"{risultato_geo['lat']:.7f}, {risultato_geo['lon']:.7f}"
                    trovati += 1

        # Salva a blocchi: così la colonna H viene realmente aggiornata
        # anche se il processo viene interrotto prima della fine.
        if (trovati - ultimo_salvataggio) >= 10:
            if salvataggio_progressivo is not None:
                try:
                    salvataggio_progressivo(risultato)
                    ultimo_salvataggio = trovati
                except Exception:
                    pass

        progress.progress(posizione / totali, text=f"🌍 Geolocalizzazione: {posizione}/{totali} clienti")

    if salvataggio_progressivo is not None and trovati > ultimo_salvataggio:
        try:
            salvataggio_progressivo(risultato)
        except Exception:
            pass

    progress.empty()
    return risultato, trovati, gia_presenti, non_trovati

# ============================================================
# V10.4.0 TEST — CARICA GIRO DA FOTO
# ------------------------------------------------------------
# Modulo isolato e conservativo (regola §25): non tocca in alcun
# modo l'ottimizzatore ne' le altre funzioni gia' funzionanti.
#
# Flusso:
#   foto -> OCR (EasyOCR, gratuito) -> riga grezza (cliente
#   grezzo + colli) -> ricerca per SOMIGLIANZA nel Foglio1 (sola
#   consultazione) -> proposta di abbinamento -> conferma manuale
#   -> creazione righe in GiroAttivo.
#
# Il Foglio1 NON viene mai scritto da questo modulo (nemmeno le
# coordinate): l'unico scopo qui e' leggere CLIENTE/COMUNE/VIA/
# ORA/ZONA/COORDINATE gia' presenti per il cliente riconosciuto.
# ============================================================

SOGLIA_MATCH_VERDE = 0.55  # sopra: abbinamento proposto come affidabile (verde)
# Due formati supportati:
# 1) tabella completa: CODICE  TRATTA  CLIENTE ... COLLI
# 2) lista semplice: CLIENTE  COLLI (es. "RILOCA GELATERIA CAFFETTE 5,00")
RIGA_OCR_PATTERN = __import__("re").compile(
    r"^\D*(\d{6,12})\s+([A-Z0-9\-]{2,15})\s+(.+?)\s+(\d{1,4})[.,](\d{2})\s*$",
    __import__("re").IGNORECASE,
)
RIGA_OCR_SEMPLICE_PATTERN = __import__("re").compile(
    r"^(.+?)\s+(\d{1,4})[.,](\d{2})\s*$",
    __import__("re").IGNORECASE,
)


@st.cache_resource(show_spinner=False)
def _ottieni_reader_easyocr():
    """Carica il modello EasyOCR una sola volta per sessione (pip puro, no apt-get)."""
    import easyocr
    return easyocr.Reader(['it'], gpu=False, verbose=False)


def _testo_da_immagine_ocr(file_bytes):
    """Esegue l'OCR (EasyOCR, motore gratuito, libreria Python pura) su una
    foto del giro serale.

    A differenza di Tesseract non richiede alcun binario di sistema ne'
    packages.txt: si installa come normale dipendenza pip (vedi requirements.txt).
    EasyOCR restituisce singole parole con coordinate; qui le raggruppiamo
    per riga (stessa fascia verticale) e le riordiniamo da sinistra a destra,
    per ricostruire lo stesso formato di testo riga-per-riga usato a valle.
    """
    try:
        from PIL import Image, ImageOps
        import numpy as np
    except ImportError:
        return None, "Librerie OCR non installate (Pillow / numpy)."

    try:
        reader = _ottieni_reader_easyocr()
    except Exception as exc:
        return None, f"Impossibile caricare il motore OCR: {exc}"

    try:
        img = Image.open(BytesIO(file_bytes))
        img = ImageOps.exif_transpose(img)  # corregge rotazioni da smartphone

        # Le foto da smartphone di tabelle piccole beneficiano di un upscaling
        # prima dell'OCR: si leggono meglio caratteri e cifre piu' grandi.
        larghezza, altezza = img.size
        if larghezza < 1800:
            fattore = min(3, max(1, round(1800 / max(larghezza, 1))))
            img = img.resize((larghezza * fattore, altezza * fattore), Image.LANCZOS)

        arr = np.array(img.convert("RGB"))
        risultati = reader.readtext(arr)
        if not risultati:
            return "", None

        # Raggruppa i blocchi di testo in righe per coordinata Y (stessa fascia).
        blocchi = []
        for bbox, testo_blocco, _conf in risultati:
            cy = sum(p[1] for p in bbox) / 4
            cx = sum(p[0] for p in bbox) / 4
            blocchi.append((cy, cx, testo_blocco))
        blocchi.sort(key=lambda b: b[0])

        TOLLERANZA_RIGA_PX = 15
        righe, riga_corrente, y_rif = [], [], None
        for cy, cx, testo_blocco in blocchi:
            if y_rif is None or abs(cy - y_rif) <= TOLLERANZA_RIGA_PX:
                riga_corrente.append((cx, testo_blocco))
                y_rif = cy if y_rif is None else (y_rif + cy) / 2
            else:
                righe.append(riga_corrente)
                riga_corrente = [(cx, testo_blocco)]
                y_rif = cy
        if riga_corrente:
            righe.append(riga_corrente)

        righe_testo = []
        for r in righe:
            r.sort(key=lambda b: b[0])  # da sinistra a destra
            righe_testo.append(" ".join(t for _, t in r))

        return "\n".join(righe_testo), None
    except Exception as exc:
        return None, f"Errore OCR: {exc}"


def _righe_grezze_da_testo_ocr(testo):
    """Interpreta il testo OCR in due formati.

    FORMATO COMPLETO:
        CODICE  TRATTA  CLIENTE  COMUNE  VIA  COLLI

    FORMATO SEMPLICE:
        CLIENTE  COLLI

    Nel formato semplice non servono codice, tratta, comune o via: il nome
    cliente viene cercato nel Foglio1 e da li' vengono recuperati COMUNE, VIA,
    ORA e COORDINATE. Questo permette di usare anche foto che contengono solo
    elenco clienti + quantita'.
    """
    righe = []
    non_riconosciute = []
    if not testo:
        return righe, non_riconosciute

    import re

    # Righe che sono chiaramente intestazioni della tabella e non clienti.
    parole_da_ignorare = {
        "loading reference", "route guide", "cod", "end address name",
        "end address city", "end address address", "quantity edu",
        "cliente", "clienti", "quantita", "quantità",
    }

    for grezza in testo.splitlines():
        grezza = re.sub(r"\s+", " ", grezza.strip())
        if not grezza:
            continue

        chiave_riga = grezza.lower().strip(" :-_")
        if chiave_riga in parole_da_ignorare or any(chiave_riga.startswith(x + " ") for x in parole_da_ignorare):
            continue

        # 1) Prima proviamo il formato completo gia' supportato.
        m = RIGA_OCR_PATTERN.match(grezza)
        if m:
            codice, tratta, testo_grezzo, colli_int, colli_dec = m.groups()
            try:
                colli = float(f"{colli_int}.{colli_dec}")
            except ValueError:
                colli = None
            righe.append({
                "codice": codice,
                "tratta": tratta,
                "testo_grezzo": testo_grezzo.strip(" —-"),
                "colli": colli,
                "riga_originale": grezza,
                "formato": "completo",
            })
            continue

        # 2) Formato semplice: CLIENTE + quantita' finale.
        m = RIGA_OCR_SEMPLICE_PATTERN.match(grezza)
        if m:
            testo_cliente, colli_int, colli_dec = m.groups()
            testo_cliente = testo_cliente.strip(" —-")

            # Evita di trattare intestazioni o righe troppo corte come clienti.
            if len(testo_cliente) < 2 or not re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]", testo_cliente):
                non_riconosciute.append(grezza)
                continue

            try:
                colli = float(f"{colli_int}.{colli_dec}")
            except ValueError:
                colli = None

            righe.append({
                "codice": "",
                "tratta": "",
                "testo_grezzo": testo_cliente,
                "colli": colli,
                "riga_originale": grezza,
                "formato": "semplice",
            })
            continue

        # Le righe senza quantita' non sono utilizzabili per costruire il giro.
        non_riconosciute.append(grezza)

    return righe, non_riconosciute


def _abbina_riga_al_database(testo_grezzo, df_db):
    """Cerca nel Foglio1 (sola consultazione) il cliente piu' simile al testo OCR.

    Ritorna (riga_db_o_None, punteggio 0-1, lista_alternative[:3]).
    Non scrive mai nulla nel database.
    """
    import difflib

    if df_db is None or df_db.empty or "CLIENTE" not in df_db.columns:
        return None, 0.0, []

    chiave_ocr = _normalizza_chiave_testo(testo_grezzo)

    candidati = []
    for _, riga in df_db.iterrows():
        chiave_db = _normalizza_chiave_testo(
            f"{riga.get('CLIENTE', '')} {riga.get('COMUNE', '')} {riga.get('VIA', '')}"
        )
        if not chiave_db:
            continue
        punteggio = difflib.SequenceMatcher(None, chiave_ocr, chiave_db).ratio()
        candidati.append((punteggio, riga))

    if not candidati:
        return None, 0.0, []

    candidati.sort(key=lambda c: c[0], reverse=True)
    migliore_punteggio, migliore_riga = candidati[0]
    alternative = [r for _, r in candidati[1:4]]
    return migliore_riga, migliore_punteggio, alternative


def costruisci_giro_da_foto(file_bytes, df_db):
    """Pipeline completa: foto -> OCR -> abbinamento -> tabella di controllo.

    Accetta sia il formato completo con codice/tratta sia la lista semplice
    composta da CLIENTE + COLLI.

    Ritorna un DataFrame con una riga per ogni cliente letto dalla foto,
    pronto per essere mostrato nella schermata "GIRO RICONOSCIUTO" prima
    della creazione definitiva del giro. Nessuna scrittura su Foglio1
    ne' su GiroAttivo avviene qui: solo lettura e proposta.
    """
    testo_ocr, errore = _testo_da_immagine_ocr(file_bytes)
    if errore:
        return None, errore

    righe_grezze, non_riconosciute = _righe_grezze_da_testo_ocr(testo_ocr)
    if not righe_grezze:
        return None, "Nessuna riga riconoscibile nella foto. Prova con una foto piu' nitida e dritta."

    risultati = []
    for riga in righe_grezze:
        match, punteggio, alternative = _abbina_riga_al_database(riga["testo_grezzo"], df_db)
        stato = "🟢" if match is not None and punteggio >= SOGLIA_MATCH_VERDE else "🟡 DA VERIFICARE"
        risultati.append({
            "Riconosciuto": stato,
            "Testo letto dalla foto": riga["testo_grezzo"],
            "Cliente abbinato": match.get("CLIENTE", "") if match is not None else "",
            "Comune": match.get("COMUNE", "") if match is not None else "",
            "Via": match.get("VIA", "") if match is not None else "",
            "Colli": riga["colli"],
            "Punteggio": round(punteggio, 2),
            "_match_zona": match.get("ZONA", "") if match is not None else "",
            "_match_ora": match.get("ORA", "") if match is not None else "",
            "_match_coordinate": match.get("COORDINATE", "") if match is not None else "",
            "_alternative_cliente": [a.get("CLIENTE", "") for a in alternative],
        })

    df_risultati = pd.DataFrame(risultati)
    avviso = None
    if non_riconosciute:
        avviso = f"{len(non_riconosciute)} riga/e della foto non e' stato possibile interpretarle come cliente + colli."
    return df_risultati, avviso


def render_carica_giro_da_foto():
    """UI isolata (TEST): carica una foto del giro serale e propone
    l'abbinamento automatico dei clienti dal Foglio1, con controllo
    manuale prima di creare/aggiungere righe in GiroAttivo.
    """
    with st.expander("📥 CARICA GIRO DELLA SERA (foto) — TEST", expanded=False):
        st.caption(
            "Carica la foto del foglio che ricevi la sera. L'app legge CLIENTE e "
            "COLLI sia dalla tabella completa sia dalla lista semplice e cerca "
            "il cliente corrispondente nel database "
            "(Foglio1, sola consultazione). Controlla sempre la tabella prima di confermare."
        )

        file_foto = st.file_uploader(
            "Foto del giro",
            type=["jpg", "jpeg", "png"],
            key="upload_foto_giro_serale",
        )

        if st.button("🔍 ANALIZZA FOTO", key="btn_analizza_foto_giro", disabled=file_foto is None):
            with st.spinner("📷 Leggo la foto e cerco i clienti nel database..."):
                df_riconosciuto, avviso = costruisci_giro_da_foto(file_foto.getvalue(), st.session_state.db_clienti)
            if df_riconosciuto is None:
                st.error(f"❌ {avviso}")
            else:
                st.session_state.foto_giro_riconosciuto = df_riconosciuto
                st.session_state.foto_giro_avviso = avviso

        df_riconosciuto = st.session_state.get("foto_giro_riconosciuto")
        if df_riconosciuto is not None and not df_riconosciuto.empty:
            avviso = st.session_state.get("foto_giro_avviso")
            if avviso:
                st.warning(f"⚠️ {avviso}")

            n_verdi = int((df_riconosciuto["Riconosciuto"] == "🟢").sum())
            st.markdown(f"**📋 GIRO RICONOSCIUTO** — {n_verdi}/{len(df_riconosciuto)} clienti abbinati automaticamente")

            righe_confermate = []
            for i, riga in df_riconosciuto.iterrows():
                cols = st.columns([0.6, 3, 1.3, 1])
                with cols[0]:
                    st.write(riga["Riconosciuto"])
                with cols[1]:
                    if riga["Riconosciuto"] == "🟢":
                        st.write(f"**{riga['Cliente abbinato']}**")
                        st.caption(f"{riga['Via']}, {riga['Comune']}")
                        cliente_scelto = riga["Cliente abbinato"]
                    else:
                        opzioni = ["— Salta questa riga —"] + [riga["Cliente abbinato"]] + list(riga.get("_alternative_cliente", []))
                        opzioni = [o for o in dict.fromkeys(opzioni) if o]
                        if not opzioni:
                            opzioni = ["— Salta questa riga —"]
                        st.caption(f"Testo letto: {riga['Testo letto dalla foto'][:60]}")
                        cliente_scelto = st.selectbox(
                            "Abbina a:", opzioni, key=f"foto_match_scelta_{i}", label_visibility="collapsed"
                        )
                        if cliente_scelto == "— Salta questa riga —":
                            cliente_scelto = None
                with cols[2]:
                    colli_scelti = st.number_input(
                        "Colli", min_value=0, value=int(riga["Colli"]) if pd.notna(riga["Colli"]) else 0,
                        key=f"foto_colli_scelti_{i}", label_visibility="collapsed"
                    )
                with cols[3]:
                    st.write("")

                if cliente_scelto:
                    match_db = st.session_state.db_clienti[
                        st.session_state.db_clienti["CLIENTE"] == cliente_scelto
                    ]
                    if not match_db.empty:
                        riga_db = match_db.iloc[0]
                        righe_confermate.append({
                            "CLIENTE": riga_db.get("CLIENTE", ""),
                            "COMUNE": riga_db.get("COMUNE", ""),
                            "VIA": riga_db.get("VIA", ""),
                            "ORA": riga_db.get("ORA", ""),
                            "Q.ta": colli_scelti,
                            "STATO": STATO_DA_FARE,
                        })

            st.markdown("---")
            c_conf, c_ann = st.columns(2)
            with c_conf:
                if st.button(
                    f"✅ CONFERMA E AGGIUNGI AL GIRO ({len(righe_confermate)})",
                    use_container_width=True, key="btn_conferma_giro_da_foto",
                    disabled=len(righe_confermate) == 0,
                ):
                    df_nuove = pd.DataFrame(righe_confermate)
                    df_nuove["POSIZIONE"] = range(
                        len(st.session_state.giro_corrente) + 1,
                        len(st.session_state.giro_corrente) + 1 + len(df_nuove)
                    )
                    st.session_state.giro_corrente = pd.concat(
                        [st.session_state.giro_corrente, df_nuove], ignore_index=True
                    )
                    salva_giro_utente_su_sheets(st.session_state.utente_corrente, st.session_state.giro_corrente)
                    st.session_state.foto_giro_riconosciuto = None
                    st.session_state.foto_giro_avviso = None
                    st.success(f"✅ {len(df_nuove)} clienti aggiunti al giro.")
                    st.rerun()
            with c_ann:
                if st.button("❌ ANNULLA", use_container_width=True, key="btn_annulla_giro_da_foto"):
                    st.session_state.foto_giro_riconosciuto = None
                    st.session_state.foto_giro_avviso = None
                    st.rerun()



# Inizializzazione Connessione Google Sheets tramite Streamlit Secrets
@st.cache_resource
def init_google_sheets():
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    client = gspread.authorize(creds)
    return client

# Connessione al foglio Google e alle relative schede
try:
    client_gs = init_google_sheets()
    sh = client_gs.open_by_key(VANGO_SPREADSHEET_ID)
    
    try:
        sheet_db = sh.worksheet("Foglio1")
    except Exception:
        sheet_db = sh.get_worksheet(0) # Fallback di sicurezza sulla prima scheda
        
    try:
        sheet_utenti = sh.worksheet("Utenti") # Seconda scheda: Utenti
    except Exception:
        sheet_utenti = None
    try:
        sheet_giro = sh.worksheet("GiroAttivo") # Terza scheda: Giro Attivo
    except Exception:
        sheet_giro = None
    try:
        sheet_registro = sh.worksheet("RegistroVisite") # Storico reale
    except Exception:
        sheet_registro = None
    try:
        sheet_statistiche = sh.worksheet("StatisticheClienti") # Statistiche elaborate
    except Exception:
        sheet_statistiche = None
    try:
        sheet_configurazione = sh.worksheet("Configurazione") # Configurazione
    except Exception:
        sheet_configurazione = None
except Exception as e:
    st.error(f"⚠️ Errore di connessione a Google Sheets: {e}")
    sheet_db = None
    sheet_utenti = None
    sheet_giro = None
    sheet_registro = None
    sheet_statistiche = None
    sheet_configurazione = None

# Funzioni per caricare e salvare gli utenti da Google Sheets (TTL ottimizzato a 300s)
@st.cache_data(ttl=300, show_spinner=False)
def carica_utenti_da_sheets():
    utenti_default = {"admin": "vango2026", "autista": "consegne2026"}
    try:
        if sheet_utenti:
            data = sheet_utenti.get_all_records()
            if data:
                dict_utenti = {}
                for row in data:
                    row_clean = {str(k).strip().upper(): str(v).strip() for k, v in row.items()}
                    usr = row_clean.get("USERNAME", "")
                    pwd = row_clean.get("PASSWORD", "")
                    if usr:
                        dict_utenti[usr] = pwd
                if dict_utenti:
                    return dict_utenti
    except Exception as e:
        st.error(f"Errore di lettura utenti da Google Sheets: {e}")
    return utenti_default

def salva_utenti_su_sheets(dict_utenti):
    """PROTEZIONE UTENTI: la scheda Utenti e' esclusivamente in lettura."""
    raise RuntimeError("Protezione VanGo: la scheda Utenti non puo' essere modificata dall'app.")

# Funzioni di utilità per i dati
def pulisci_orario(valore):
    if pd.isna(valore):
        return ""
    val_str = str(valore).strip()
    if 'days' in val_str:
        val_str = val_str.split()[-1]
    if ' ' in val_str:
        val_str = val_str.split()[-1]
    if len(val_str) >= 5:
        return val_str[:5]
    return val_str

def elabora_dataframe_db(df):
    if df.empty:
        return pd.DataFrame(columns=['POSIZIONE', 'ZONA', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'QTA_DEFAULT', 'COORDINATE'])
    
    df.columns = df.columns.str.strip().str.upper()
    
    if 'POSIZIONE' in df.columns:
        df['POSIZIONE'] = pd.to_numeric(df['POSIZIONE'], errors='coerce').fillna(0).astype(int)
    else:
        df['POSIZIONE'] = range(1, len(df) + 1)
        
    if 'QTA_DEFAULT' in df.columns:
        df['QTA_DEFAULT'] = pd.to_numeric(df['QTA_DEFAULT'], errors='coerce').fillna(0).astype(int)
    else:
        df['QTA_DEFAULT'] = 0

    for col in ['ZONA', 'CLIENTE', 'COMUNE', 'VIA', 'COORDINATE']:
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str).str.strip()
        else:
            df[col] = ""

    if 'ORA' in df.columns:
        df['ORA'] = df['ORA'].apply(pulisci_orario)
    else:
        df['ORA'] = ""
        
    return df.sort_values(by="POSIZIONE").reset_index(drop=True)

def salva_coordinate_su_google_sheets(df):
    """Aggiorna SOLO la colonna H del Foglio1, senza cancellare il database."""
    try:
        if not sheet_db or df is None or df.empty:
            return False

        # Assicura l'intestazione H1.
        try:
            sheet_db.update("H1", [["COORDINATE"]])
        except Exception:
            pass

        valori = []
        for valore in df["COORDINATE"].tolist() if "COORDINATE" in df.columns else []:
            valori.append(["" if pd.isna(valore) else str(valore)])

        if valori:
            # Riga 1 = intestazione, quindi il primo cliente è H2.
            sheet_db.update(f"H2:H{len(valori) + 1}", valori)
        st.cache_data.clear()
        return True
    except Exception as e:
        st.warning(f"⚠️ Salvataggio coordinate in colonna H non riuscito: {e}")
        return False


def salva_db_su_google_sheets(df):
    """PROTEZIONE DATABASE: Foglio1 non viene mai riscritto.
    L'unica scrittura consentita su Foglio1 e' la colonna H (COORDINATE).
    Questa funzione resta solo per compatibilita' con vecchio codice e inoltra
    esclusivamente il salvataggio della colonna H.
    """
    return salva_coordinate_su_google_sheets(df)

# Database Clienti con TTL ottimizzato a 300s
@st.cache_data(ttl=300, show_spinner=False)
def carica_db_da_google_sheets_cached():
    try:
        if sheet_db:
            valori_grezzi = sheet_db.get_all_values()
            if not valori_grezzi:
                # Foglio1 e' protetto: non riscriviamo mai l'intestazione completa.
                # L'unica cella che l'app puo' aggiornare e' H1/H2:H (COORDINATE).
                intestazioni_default = ['POSIZIONE', 'ZONA', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'QTA_DEFAULT', 'COORDINATE']
                return pd.DataFrame(columns=intestazioni_default)
            
            data = sheet_db.get_all_records()
            if data:
                df = pd.DataFrame(data)
                return elabora_dataframe_db(df)
    except Exception as e:
        st.error(f"Errore di lettura da Google Sheets: {e}")
    return pd.DataFrame(columns=['POSIZIONE', 'ZONA', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'QTA_DEFAULT', 'COORDINATE'])

def carica_db_da_google_sheets():
    return carica_db_da_google_sheets_cached()

# ==========================================
# ANALISI - lettura di RegistroVisite e StatisticheClienti
# ==========================================
# ANALISI e' esclusivamente di lettura: non modifica i fogli dati.

def _normalizza_colonne_analisi(df):
    if df is None or df.empty:
        return pd.DataFrame()
    out = df.copy()
    out.columns = [str(c).strip() for c in out.columns]
    return out

@st.cache_data(ttl=120, show_spinner=False)
def carica_registro_visite_da_sheets():
    try:
        if sheet_registro:
            data = sheet_registro.get_all_records()
            if data:
                return _normalizza_colonne_analisi(pd.DataFrame(data))
    except Exception:
        pass
    return pd.DataFrame()

@st.cache_data(ttl=120, show_spinner=False)
def carica_statistiche_clienti_da_sheets():
    try:
        if sheet_statistiche:
            data = sheet_statistiche.get_all_records()
            if data:
                return _normalizza_colonne_analisi(pd.DataFrame(data))
    except Exception:
        pass
    return pd.DataFrame()

def _colonna_analisi(df, candidati):
    if df is None or df.empty:
        return None
    normal = {str(c).strip().upper(): c for c in df.columns}
    for nome in candidati:
        if str(nome).strip().upper() in normal:
            return normal[str(nome).strip().upper()]
    return None

def _serie_numerica_analisi(df, candidati):
    c = _colonna_analisi(df, candidati)
    if c is None:
        return pd.Series(0.0, index=df.index)
    return pd.to_numeric(df[c], errors='coerce').fillna(0.0)

def _serie_data_analisi(df):
    c = _colonna_analisi(df, ['DATA','DATA_VISITA','DATA VISITA','GIORNO','DATE','DATA_ARRIVO','DATA ARRIVO','TIMESTAMP','DATETIME'])
    if c is None:
        return pd.Series(pd.NaT, index=df.index)
    return pd.to_datetime(df[c], errors='coerce', dayfirst=True)

def _minuti_da_colonne_analisi(df, candidati):
    vals = _serie_numerica_analisi(df, candidati)
    if not vals.empty and vals.max() > 0 and vals.max() < 1:
        return vals * 1440.0
    return vals

def _formatta_hm_analisi(minuti):
    try:
        minuti = max(0.0, float(minuti))
    except Exception:
        minuti = 0.0
    h = int(minuti // 60)
    m = int(round(minuti - h * 60))
    if m >= 60:
        h += 1; m = 0
    return f"{h}h {m:02d}m" if h else f"{m} min"

def _formatta_numero_analisi(valore, decimali=1):
    try:
        x = float(valore)
        if abs(x-round(x)) < 1e-9:
            return f"{int(round(x)):,}".replace(',', '.')
        return f"{x:.{decimali}f}".replace('.', ',')
    except Exception:
        return '0'

def _metriche_analisi_oggi(df, oggi=None):
    if df is None or df.empty:
        return {'visite':0,'completati':0,'consegnati':0,'rifiutati':0,'strada':0.0,'servizio':0.0,'fermo':0.0,'totale':0.0,'previsto':0.0,'reale':0.0}
    oggi = oggi or datetime.now().date()
    date = _serie_data_analisi(df)
    d = df[date.dt.date == oggi].copy() if date.notna().any() else df.copy()
    stato = _colonna_analisi(d, ['STATO','STATO_CONSEGNA','ESITO'])
    completati = int(d[stato].fillna('').astype(str).str.strip().ne('').sum()) if stato else len(d)
    consegnati = _serie_numerica_analisi(d, ['COLLI_CONSEGNATI','COLLI CONSEGNATI','COLLI','QTA_CONSEGNATA','QTA CONSEGNATA']).sum()
    rifiutati = _serie_numerica_analisi(d, ['COLLI_RIFIUTATI','COLLI RIFIUTATI','COLLI_DA_RENDERE','COLLI DA RENDERE','RESI','RIFIUTATI']).sum()
    strada = _minuti_da_colonne_analisi(d, ['MINUTI_STRADA','TEMPO_STRADA','TEMPO STRADA','MIN_STRADA']).sum()
    servizio = _minuti_da_colonne_analisi(d, ['MINUTI_SERVIZIO','TEMPO_SERVIZIO','TEMPO SERVIZIO','MIN_SERVIZIO']).sum()
    fermo = _minuti_da_colonne_analisi(d, ['MINUTI_FERMO','FERMO_MEZZO','TEMPO_FERMO','TEMPO FERMO']).sum()
    totale = _minuti_da_colonne_analisi(d, ['MINUTI_TOTALI','TEMPO_TOTALE','TEMPO TOTALE','TEMPO_REALE','TEMPO REALE']).sum()
    previsto = _minuti_da_colonne_analisi(d, ['MINUTI_PREVISTI','TEMPO_PREVISTO','TEMPO PREVISTO','PREVISTO']).sum()
    reale = _minuti_da_colonne_analisi(d, ['MINUTI_REALI','TEMPO_REALE','TEMPO REALE','MINUTI_TOTALI']).sum()
    if totale == 0: totale = strada + servizio
    if reale == 0: reale = totale
    return {'visite':len(d),'completati':completati,'consegnati':consegnati,'rifiutati':rifiutati,'strada':strada,'servizio':servizio,'fermo':fermo,'totale':totale,'previsto':previsto,'reale':reale}

def _metriche_analisi_mese(df):
    if df is None or df.empty:
        return {'visite':0,'colli':0.0,'resi':0.0,'tempo_medio':0.0,'colli_medi':0.0}
    oggi = datetime.now(); date = _serie_data_analisi(df)
    d = df[(date.dt.year == oggi.year) & (date.dt.month == oggi.month)].copy() if date.notna().any() else df.copy()
    colli = _serie_numerica_analisi(d, ['COLLI_CONSEGNATI','COLLI CONSEGNATI','COLLI','QTA_CONSEGNATA','QTA CONSEGNATA']).sum()
    resi = _serie_numerica_analisi(d, ['COLLI_RIFIUTATI','COLLI RIFIUTATI','COLLI_DA_RENDERE','COLLI DA RENDERE','RESI','RIFIUTATI']).sum()
    tempi = _minuti_da_colonne_analisi(d, ['MINUTI_SERVIZIO','TEMPO_SERVIZIO','TEMPO SERVIZIO','MINUTI_REALI','TEMPO_REALE','TEMPO REALE'])
    return {'visite':len(d),'colli':colli,'resi':resi,'tempo_medio':float(tempi.mean()) if len(tempi) else 0.0,'colli_medi':float(colli)/len(d) if len(d) else 0.0}

def _classifiche_clienti_analisi(df):
    if df is None or df.empty: return pd.DataFrame(columns=['CLIENTE','VISITE','COLLI','RESI','TEMPO'])
    cc = _colonna_analisi(df, ['CLIENTE','NOME CLIENTE'])
    if cc is None: return pd.DataFrame(columns=['CLIENTE','VISITE','COLLI','RESI','TEMPO'])
    x=df.copy(); x['__CLIENTE']=x[cc].fillna('').astype(str).str.strip(); x=x[x['__CLIENTE']!='']
    x['__COLLI']=_serie_numerica_analisi(x,['COLLI_CONSEGNATI','COLLI CONSEGNATI','COLLI','QTA_CONSEGNATA','QTA CONSEGNATA'])
    x['__RESI']=_serie_numerica_analisi(x,['COLLI_RIFIUTATI','COLLI RIFIUTATI','COLLI_DA_RENDERE','COLLI DA RENDERE','RESI','RIFIUTATI'])
    x['__TEMPO']=_minuti_da_colonne_analisi(x,['MINUTI_SERVIZIO','TEMPO_SERVIZIO','TEMPO SERVIZIO','MINUTI_REALI','TEMPO_REALE','TEMPO REALE'])
    return x.groupby('__CLIENTE').agg(VISITE=('__CLIENTE','size'),COLLI=('__COLLI','sum'),RESI=('__RESI','sum'),TEMPO=('__TEMPO','sum')).reset_index().rename(columns={'__CLIENTE':'CLIENTE'}).sort_values('VISITE',ascending=False).reset_index(drop=True)

def _render_card_analisi(titolo,valore,sottotitolo=''):
    st.markdown(f"<div style=\"border:1px solid rgba(148,163,184,.25);border-radius:14px;padding:15px 16px;background:rgba(30,41,59,.20);min-height:105px;\"><div style=\"font-size:12px;color:#94A3B8;text-transform:uppercase;font-weight:700;\">{titolo}</div><div style=\"font-size:27px;font-weight:800;margin-top:7px;\">{valore}</div><div style=\"font-size:12px;color:#94A3B8;margin-top:4px;\">{sottotitolo}</div></div>", unsafe_allow_html=True)

def render_analisi():
    st.markdown('## 📊 ANALISI')
    st.caption('Lettura dei dati reali di RegistroVisite e delle statistiche elaborate. Questa pagina non modifica i dati.')
    registro=carica_registro_visite_da_sheets(); statistiche=carica_statistiche_clienti_da_sheets()
    if registro.empty and statistiche.empty:
        st.info('📭 Non ci sono ancora dati disponibili in RegistroVisite o StatisticheClienti.')
        if st.button('🔄 AGGIORNA DATI ANALISI',use_container_width=True,key='btn_refresh_analisi_empty'): st.cache_data.clear(); st.rerun()
        return
    oggi=_metriche_analisi_oggi(registro); mese=_metriche_analisi_mese(registro); classifiche=_classifiche_clienti_analisi(registro)
    st.markdown('### OGGI'); c=st.columns(4)
    for col,t,v in zip(c,['Clienti visitati','Clienti completati','Colli consegnati','Colli rifiutati'],[oggi['visite'],oggi['completati'],oggi['consegnati'],oggi['rifiutati']]):
        with col: _render_card_analisi(t,_formatta_numero_analisi(v))
    c=st.columns(5)
    vals=[('Tempo strada',_formatta_hm_analisi(oggi['strada'])),('Tempo servizio',_formatta_hm_analisi(oggi['servizio'])),('Fermo mezzo',_formatta_hm_analisi(oggi['fermo'])),('Tempo totale',_formatta_hm_analisi(oggi['totale']))]
    for col,(t,v) in zip(c[:4],vals):
        with col: _render_card_analisi(t,v)
    scarto=oggi['reale']-oggi['previsto'] if oggi['previsto'] else 0; confronto='Nessun previsto registrato' if not oggi['previsto'] else (('+' if scarto>=0 else '-')+_formatta_hm_analisi(abs(scarto)))
    with c[4]: _render_card_analisi('Previsto vs reale',confronto)
    st.markdown('### QUESTO MESE'); c=st.columns(5)
    vals=[('Numero visite',_formatta_numero_analisi(mese['visite'])),('Totale colli',_formatta_numero_analisi(mese['colli'])),('Totale resi',_formatta_numero_analisi(mese['resi'])),('Tempo medio / cliente',_formatta_hm_analisi(mese['tempo_medio'])),('Colli medi / visita',_formatta_numero_analisi(mese['colli_medi'],1))]
    for col,(t,v) in zip(c,vals):
        with col: _render_card_analisi(t,v)
    st.markdown('### CLIENTI')
    if classifiche.empty: st.info('Nessun cliente disponibile nello storico.')
    else:
        cols=st.columns(5)
        for col,tit,sc,asc in zip(cols,['🥇 PIÙ VISITATI','📦 PIÙ COLLI','↩️ PIÙ RESI','⏱️ PIÙ TEMPO RICHIESTO','⚡ PIÙ VELOCI'],['VISITE','COLLI','RESI','TEMPO','TEMPO'],[False,False,False,False,True]):
            with col:
                st.markdown(f'**{tit}**'); top=classifiche.sort_values(sc,ascending=asc).head(5)[['CLIENTE',sc]].copy(); top.columns=['CLIENTE','VALORE']; st.dataframe(top,hide_index=True,use_container_width=True,height=225)
    st.markdown('### 🔎 CERCA CLIENTE')
    nomi=sorted(classifiche['CLIENTE'].astype(str).unique().tolist()) if not classifiche.empty else []
    cliente=st.selectbox('Seleziona cliente',['']+nomi,key='analisi_cliente_select')
    if cliente:
        r=classifiche[classifiche['CLIENTE'].astype(str)==cliente].iloc[0]; st.markdown(f'### {cliente}'); c=st.columns(4)
        for col,t,v in zip(c,['Visite','Colli consegnati','Colli rifiutati','Tempo medio'],[r['VISITE'],r['COLLI'],r['RESI'],float(r['TEMPO'])/float(r['VISITE']) if float(r['VISITE']) else 0]):
            with col: _render_card_analisi(t,_formatta_numero_analisi(v) if t!='Tempo medio' else _formatta_hm_analisi(v))
        if not registro.empty:
            cc=_colonna_analisi(registro,['CLIENTE','NOME CLIENTE']); mask=registro[cc].fillna('').astype(str).str.strip().eq(cliente) if cc else pd.Series(False,index=registro.index); storico=registro.loc[mask].copy(); dd=_serie_data_analisi(registro).loc[mask]; storico['__DATA']=dd; storico['__TEMPO']=_minuti_da_colonne_analisi(storico,['MINUTI_SERVIZIO','TEMPO_SERVIZIO','TEMPO SERVIZIO','MINUTI_REALI','TEMPO_REALE','TEMPO REALE']); storico=storico.sort_values('__DATA',ascending=False); righe=pd.DataFrame({'DATA':storico['__DATA'].dt.strftime('%d/%m/%Y').fillna(''),'TEMPO':storico['__TEMPO'].map(_formatta_hm_analisi)}); st.markdown('**Ultime visite**'); st.dataframe(righe.head(10),hide_index=True,use_container_width=True)
    st.markdown('### 🧠 AUTOAPPRENDIMENTO'); st.info("Sezione predisposta per la FASE 5: tempo standard, osservazioni, media reale e tempi in funzione dei colli verranno collegati allo storico quando attiveremo l'autoapprendimento.")
    if st.button('🔄 AGGIORNA DATI ANALISI',use_container_width=True,key='btn_refresh_analisi'): st.cache_data.clear(); st.rerun()

# --- Gestione Giro per singolo utente su Google Sheets (TTL ottimizzato a 120s) ---
@st.cache_data(ttl=120, show_spinner=False)
def carica_tutti_i_giri_da_sheets():
    try:
        if sheet_giro:
            data = sheet_giro.get_all_records()
            if data:
                return pd.DataFrame(data)
    except Exception as e:
        pass
    return pd.DataFrame(columns=['UTENTE', 'POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta', 'COLLI_CONSEGNATI', 'COLLI_RIFIUTATI', 'COLLI_DA_RENDERE', 'STATO', 'MIN_TRATTA_PREVISTA', 'MIN_PREVISTI_CUMULATIVI', 'TIPO_RIGA', 'BACKUP_JSON'])

def carica_giro_utente_da_sheets(nome_utente):
    cols_giro = ['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta', 'COLLI_CONSEGNATI', 'COLLI_RIFIUTATI', 'COLLI_DA_RENDERE', 'STATO', 'MIN_TRATTA_PREVISTA', 'MIN_PREVISTI_CUMULATIVI']
    df_vuoto = pd.DataFrame(columns=cols_giro)
    try:
        df = carica_tutti_i_giri_da_sheets()
        if df.empty:
            return df_vuoto

        df.columns = df.columns.astype(str).str.strip().str.upper()
        if 'UTENTE' not in df.columns:
            return df_vuoto

        nome = nome_utente.strip().lower()
        df_utente = df[df['UTENTE'].astype(str).str.strip().str.lower() == nome].copy()

        # Il giro normale ha priorita'. Le righe BACKUP sono tecniche e non
        # devono mai essere mostrate come clienti.
        if not df_utente.empty:
            if 'Q.TA' in df_utente.columns:
                df_utente = df_utente.rename(columns={'Q.TA': 'Q.ta'})
            for c in cols_giro:
                if c not in df_utente.columns:
                    df_utente[c] = ''
            df_utente = df_utente[cols_giro]
            df_utente['STATO'] = df_utente['STATO'].fillna('').astype(str)
            if not df_utente.empty:
                df_utente['POSIZIONE'] = [str(i) for i in range(1, len(df_utente) + 1)]
                return df_utente.reset_index(drop=True)

        # FALLBACK DI SICUREZZA:
        # se per qualsiasi motivo il giro normale non c'e' piu', recuperiamo il
        # giro completo dalla riga __VANGO_BACKUP__::utente. Questo impedisce che
        # una riapertura dell'app perda il giro anche se restano solo le righe tecniche.
        backup_utente = BACKUP_UTENTE_PREFIX + str(nome_utente).strip()
        righe_backup = df[df['UTENTE'].astype(str).str.strip().str.lower() == backup_utente.lower()]
        if not righe_backup.empty:
            payload = str(righe_backup.iloc[-1].get('BACKUP_JSON', '') or '').strip()
            if payload:
                try:
                    snapshot = json.loads(payload)
                    if isinstance(snapshot, dict) and snapshot.get('tipo') == 'GIRO_COMPLETO':
                        righe = snapshot.get('righe', [])
                        if righe:
                            recuperato = pd.DataFrame(righe).copy()
                            if '__VANGO_POSIZIONE_BACKUP' in recuperato.columns:
                                recuperato = recuperato.sort_values('__VANGO_POSIZIONE_BACKUP', kind='stable')
                                recuperato = recuperato.drop(columns=['__VANGO_POSIZIONE_BACKUP'])
                            for c in cols_giro:
                                if c not in recuperato.columns:
                                    recuperato[c] = ''
                            if 'Q.TA' in recuperato.columns and 'Q.ta' not in recuperato.columns:
                                recuperato = recuperato.rename(columns={'Q.TA': 'Q.ta'})
                            recuperato = recuperato[cols_giro].reset_index(drop=True)
                            recuperato['POSIZIONE'] = [str(i) for i in range(1, len(recuperato) + 1)]
                            return recuperato
                except Exception:
                    pass
    except Exception as e:
        st.error(f"Errore di lettura del giro da Google Sheets: {e}")
    return df_vuoto

def salva_giro_utente_su_sheets(nome_utente, df_nuovo_giro):
    """Salva il giro esclusivamente su GiroAttivo.

    Foglio1 e Utenti non vengono mai modificati da questa funzione.
    Le eventuali righe tecniche di backup presenti in GiroAttivo vengono mantenute.
    """
    cols_ordine = ['UTENTE', 'POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta', 'COLLI_CONSEGNATI', 'COLLI_RIFIUTATI', 'COLLI_DA_RENDERE', 'STATO', 'MIN_TRATTA_PREVISTA', 'MIN_PREVISTI_CUMULATIVI', 'TIPO_RIGA', 'BACKUP_JSON']
    for tentativo in range(5):
        try:
            if sheet_giro:
                time.sleep(1.5 * (tentativo + 1))

                data_totale = sheet_giro.get_all_records()
                df_tutti = pd.DataFrame(data_totale) if data_totale else pd.DataFrame(columns=cols_ordine)

                if not df_tutti.empty:
                    df_tutti.columns = df_tutti.columns.str.strip().str.upper()
                    if 'Q.TA' in df_tutti.columns:
                        df_tutti = df_tutti.rename(columns={'Q.TA': 'Q.ta'})
                    for c in cols_ordine:
                        if c not in df_tutti.columns:
                            df_tutti[c] = ""
                    df_tutti = df_tutti[cols_ordine]
                    # Rimuove solo il giro normale dell'utente corrente.
                    # Le righe tecniche di backup vengono preservate.
                    mask_utente = df_tutti['UTENTE'].astype(str).str.strip().str.lower() == nome_utente.strip().lower()
                    df_tutti = df_tutti.loc[~mask_utente].copy()

                if not df_nuovo_giro.empty:
                    df_agg = df_nuovo_giro.copy()
                    df_agg['UTENTE'] = nome_utente
                    df_agg['POSIZIONE'] = range(1, len(df_agg) + 1)
                    for c in cols_ordine:
                        if c not in df_agg.columns:
                            df_agg[c] = ""
                    df_agg['TIPO_RIGA'] = ""
                    df_agg['BACKUP_JSON'] = ""
                    df_agg = df_agg[cols_ordine]
                    df_tutti = pd.concat([df_tutti, df_agg], ignore_index=True)

                _scrivi_giro_su_sheets_sicuro(df_tutti, cols_ordine)

                st.cache_data.clear()
                return True
        except Exception as e:
            if "429" in str(e) and tentativo < 4:
                continue
            if tentativo == 4:
                st.error(f"Errore nel salvataggio del giro su Google Sheets dopo vari tentativi: {e}")
            else:
                st.error(f"Errore nel salvataggio del giro su Google Sheets: {e}")
                break
    return False


# ============================================================
# GPS LIVE SMARTPHONE - V10.5.1 TEST
# ============================================================
GPS_UTENTE_PREFIX = "__VANGO_GPS__::"
GPS_REVERSE_URL = "https://nominatim.openstreetmap.org/reverse"


def _gps_utente(nome_utente):
    return f"{GPS_UTENTE_PREFIX}{str(nome_utente).strip()}"


def salva_posizione_gps_su_sheets(nome_utente, posizione):
    """Salva SOLO l'ultima posizione GPS del conducente in GiroAttivo.

    La posizione viene mantenuta in una riga tecnica separata e non viene mai
    interpretata come cliente del giro. In produzione questa parte potra'
    essere spostata su un backend dedicato senza cambiare l'interfaccia GPS.
    """
    if not sheet_giro or not nome_utente or not posizione:
        return False
    try:
        import json as _json
        payload = _json.dumps(_json_sicuro(posizione), ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        utente_gps = _gps_utente(nome_utente)
        cols = ['UTENTE', 'POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta',
                'COLLI_CONSEGNATI', 'COLLI_RIFIUTATI', 'COLLI_DA_RENDERE', 'STATO',
                'MIN_TRATTA_PREVISTA', 'MIN_PREVISTI_CUMULATIVI', 'TIPO_RIGA', 'BACKUP_JSON']
        valori = sheet_giro.get_all_values()
        if not valori:
            sheet_giro.append_row(cols, value_input_option="USER_ENTERED")
            valori = [cols]
        header = [str(x).strip() for x in valori[0]]
        # Se il foglio non ha ancora tutte le colonne tecniche, non tocchiamo
        # la struttura: la posizione verra' salvata solo se le colonne esistono.
        if not all(c in header for c in cols):
            return False
        indice_utente = header.index('UTENTE')
        riga_esistente = None
        for n, riga in enumerate(valori[1:], start=2):
            if len(riga) > indice_utente and str(riga[indice_utente]).strip().lower() == utente_gps.lower():
                riga_esistente = n
                break
        nuova = {c: '' for c in cols}
        # Salviamo anche l'indirizzo leggibile ottenuto dal reverse geocoding.
        # In GiroAttivo: VIA = strada (+ numero civico), COMUNE = paese/citta'.
        nuova.update({
            'UTENTE': utente_gps,
            'POSIZIONE': str(posizione.get('timestamp_iso', '')),
            'CLIENTE': 'GPS LIVE',
            'COMUNE': str(posizione.get('comune', '') or ''),
            'VIA': str(posizione.get('via', '') or ''),
            'TIPO_RIGA': 'GPS_LIVE',
            'BACKUP_JSON': payload,
        })
        valori_riga = [nuova[c] for c in cols]
        if riga_esistente is None:
            sheet_giro.append_row(valori_riga, value_input_option="USER_ENTERED")
        else:
            ultima_col = chr(64 + len(cols)) if len(cols) <= 26 else 'O'
            sheet_giro.update(f"A{riga_esistente}:{ultima_col}{riga_esistente}", [valori_riga])
        return True
    except Exception:
        return False


def _acquisisci_gps_e_salva():
    """Acquisisce la posizione corrente dal browser e salva l'ultima lettura.

    Il componente streamlit-js-eval usa una chiave fissa e dedicata al GPS.
    In questo modo non vengono create piu' istanze di getLocation() durante
    i rerun/refresh del fragment e il browser mantiene una sola richiesta
    di geolocalizzazione attiva.
    """
    if get_geolocation is None:
        st.session_state.gps_errore = "Modulo GPS non installato."
        return False
    try:
        # Manteniamo UNA SOLA chiave del componente GPS.
        # streamlit-js-eval riesegue l'espressione solo quando il testo
        # dell'espressione cambia; aggiungiamo quindi un nonce innocuo
        # ad ogni ciclo dei 60 secondi, senza creare nuove chiavi.
        # In questo modo evitiamo sia il warning "same key=getLocation()"
        # sia la creazione continua di iframe/componenti GPS.
        st.session_state.gps_component_counter = int(
            st.session_state.get('gps_component_counter', 0)
        ) + 1
        gps_nonce = st.session_state.gps_component_counter
        gps_js_expression = f"getLocation() /* vango_gps_refresh_{gps_nonce} */"
        loc = streamlit_js_eval(js_expressions=gps_js_expression, key="vango_gps_live", want_output=True)
        if not loc:
            st.session_state.gps_errore = 'Il telefono non ha ancora restituito la posizione GPS. Verifica il permesso di posizione del browser e attendi qualche secondo.'
            return False
        if 'error' in loc:
            err = loc.get('error', {})
            st.session_state.gps_errore = str(err.get('message', 'Posizione non disponibile.'))
            return False
        coords = loc.get('coords', loc)
        lat = coords.get('latitude')
        lon = coords.get('longitude')
        if lat is None or lon is None:
            st.session_state.gps_errore = "Il browser non ha restituito coordinate valide."
            return False
        accuracy = coords.get('accuracy')
        timestamp = loc.get('timestamp') or time.time() * 1000
        timestamp_sec = float(timestamp) / 1000.0 if float(timestamp) > 10000000000 else float(timestamp)
        posizione = {
            'latitude': float(lat),
            'longitude': float(lon),
            'accuracy_m': float(accuracy) if accuracy is not None else None,
            'timestamp': timestamp_sec,
            'timestamp_iso': datetime.fromtimestamp(timestamp_sec).isoformat(timespec='seconds'),
            'via': '',
            'comune': '',
        }

        # Reverse geocoding gratuito: trasformiamo le coordinate GPS in
        # indirizzo leggibile da mostrare nel riquadro POSIZIONE ATTUALE.
        try:
            headers = {'User-Agent': 'VanGo-GPS/1.0'}
            risposta = requests.get(
                GPS_REVERSE_URL,
                params={
                    'lat': posizione['latitude'],
                    'lon': posizione['longitude'],
                    'format': 'jsonv2',
                    'zoom': 18,
                    'addressdetails': 1,
                    'accept-language': 'it',
                },
                headers=headers,
                timeout=8,
            )
            if risposta.status_code == 200:
                indirizzo = risposta.json().get('address', {}) or {}
                via = indirizzo.get('road') or indirizzo.get('pedestrian') or indirizzo.get('footway') or indirizzo.get('path') or ''
                numero = indirizzo.get('house_number') or ''
                comune = (indirizzo.get('city') or indirizzo.get('town') or indirizzo.get('village')
                          or indirizzo.get('municipality') or indirizzo.get('city_district') or '')
                posizione['via'] = f"{via} {numero}".strip() if via else ''
                posizione['comune'] = str(comune).strip()
                posizione['display_name'] = risposta.json().get('display_name', '')
        except Exception:
            pass
        st.session_state.gps_latitudine = posizione['latitude']
        st.session_state.gps_longitudine = posizione['longitude']
        st.session_state.gps_accuracy = posizione['accuracy_m']
        st.session_state.gps_timestamp = posizione['timestamp']
        st.session_state.gps_errore = None
        st.session_state.gps_via = posizione.get('via', '')
        st.session_state.gps_comune = posizione.get('comune', '')
        salva_posizione_gps_su_sheets(st.session_state.utente_corrente, posizione)
        return True
    except Exception as e:
        st.session_state.gps_errore = str(e)
        return False



if hasattr(st, 'fragment'):
    @st.fragment(run_every="60s")
    def _gps_live_refresh():
        if (st.session_state.get('gps_attivo', False)
                and not st.session_state.get('giro_terminato', False)):
            _acquisisci_gps_e_salva()

            if st.session_state.get('gps_latitudine') is not None and st.session_state.get('gps_longitudine') is not None:
                gps_df = pd.DataFrame([{
                    'lat': st.session_state.gps_latitudine,
                    'lon': st.session_state.gps_longitudine,
                }])
                st.map(gps_df, latitude='lat', longitude='lon', zoom=15, height=220)

            if st.session_state.get('gps_errore'):
                st.warning(f"📍 GPS: {st.session_state.gps_errore}")

            if st.session_state.get('gps_latitudine') is not None:
                acc = st.session_state.get('gps_accuracy')
                acc_txt = f"±{acc:.0f} m" if isinstance(acc, (int, float)) else "accuratezza n/d"
                ts = st.session_state.get('gps_timestamp')
                ora_gps = datetime.fromtimestamp(float(ts)).strftime('%H:%M:%S') if ts else "--:--:--"
                st.metric("Ultima posizione", ora_gps, acc_txt)

            st.caption("FASE TEST: la posizione viene salvata come ultima posizione GPS del conducente. Il tracking continua finche' questa pagina resta aperta.")
else:
    def _gps_live_refresh():
        if (st.session_state.get('gps_attivo', False)
                and not st.session_state.get('giro_terminato', False)):
            _acquisisci_gps_e_salva()

BACKUP_UTENTE_PREFIX = "__VANGO_BACKUP__::"
GIRO_META_PREFIX = "__VANGO_META__::"

def _meta_utente_giro(nome_utente):
    return f"{GIRO_META_PREFIX}{str(nome_utente).strip()}"

def carica_stato_giro_persistente(nome_utente):
    """Legge lo stato tecnico del giro da GiroAttivo, senza modificare Foglio1/Utenti."""
    risultato = {"giro_terminato": False, "inizio_giro_reale": None, "fine_giro_reale": None, "previsione_giro": None,
                 "fermo_mezzo_attivo": False, "inizio_fermo_mezzo": None, "minuti_fermo_mezzo": 0.0}
    try:
        df = carica_tutti_i_giri_da_sheets()
        if df.empty:
            return risultato
        df.columns = df.columns.str.strip().str.upper()
        if "UTENTE" not in df.columns:
            return risultato
        target = _meta_utente_giro(nome_utente).strip().lower()
        righe = df[df["UTENTE"].astype(str).str.strip().str.lower() == target]
        if righe.empty:
            return risultato
        row = righe.iloc[-1]
        raw = str(row.get("BACKUP_JSON", "") or "").strip()
        if not raw:
            return risultato
        import json as _json
        dati = _json.loads(raw)
        risultato["giro_terminato"] = bool(dati.get("giro_terminato", False))
        risultato["inizio_giro_reale"] = dati.get("inizio_giro_reale")
        risultato["fine_giro_reale"] = dati.get("fine_giro_reale")
        risultato["previsione_giro"] = dati.get("previsione_giro")
        risultato["fermo_mezzo_attivo"] = bool(dati.get("fermo_mezzo_attivo", False))
        risultato["inizio_fermo_mezzo"] = dati.get("inizio_fermo_mezzo")
        risultato["minuti_fermo_mezzo"] = float(dati.get("minuti_fermo_mezzo", 0) or 0)
    except Exception:
        pass
    return risultato

def _json_sicuro(dato):
    """Converte ricorsivamente NaN/NaT e scalar numpy in valori JSON validi."""
    if dato is None:
        return None
    if isinstance(dato, float) and (pd.isna(dato)):
        return None
    if isinstance(dato, (pd.Timestamp, datetime, date)):
        return dato.isoformat()
    if hasattr(dato, "item"):
        try:
            return _json_sicuro(dato.item())
        except Exception:
            pass
    if isinstance(dato, dict):
        return {str(k): _json_sicuro(v) for k, v in dato.items()}
    if isinstance(dato, (list, tuple)):
        return [_json_sicuro(v) for v in dato]
    try:
        if pd.isna(dato):
            return None
    except Exception:
        pass
    return dato


def _scrivi_giro_su_sheets_sicuro(df_dati, cols_ordine):
    """Scrive GiroAttivo senza svuotarlo prima dell'aggiornamento.

    Prima viene scritto il nuovo contenuto nell'area necessaria; solo dopo un
    aggiornamento riuscito vengono eliminate eventuali righe vecchie in coda.
    In questo modo un errore Google Sheets/429 non puo' lasciare GiroAttivo vuoto.
    """
    if not sheet_giro:
        return False
    # Google Sheets non accetta NaN/NaT/numpy scalar fuori JSON.
    # Convertiamo ogni cella in un valore JSON-safe prima della chiamata gspread.
    df_safe = df_dati.copy()
    df_safe = df_safe.where(pd.notna(df_safe), "")
    dati = [cols_ordine] + [
        [
            ("" if pd.isna(v) else (v.item() if hasattr(v, "item") else v))
            for v in row
        ]
        for row in df_safe.itertuples(index=False, name=None)
    ]
    # gspread usa 1-based row/column. Costruiamo l'ultima cella della nuova area.
    n_rows = len(dati)
    n_cols = len(cols_ordine)
    col = n_cols
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    ultima_cella = f"{letters}{n_rows}"

    # IMPORTANTE: prima update, senza clear preventivo.
    sheet_giro.update(dati, "A1")

    # Se il vecchio foglio era piu' lungo, puliamo solo la coda ormai obsoleta.
    try:
        righe_attuali = len(sheet_giro.get_all_values())
        if righe_attuali > n_rows:
            sheet_giro.batch_clear([f"A{n_rows + 1}:{letters}{righe_attuali}"])
    except Exception:
        # La scrittura principale e' gia' riuscita: una mancata pulizia della
        # coda non deve far fallire il salvataggio del giro.
        pass
    return True

def salva_stato_giro_persistente(nome_utente):
    """Memorizza lo stato di TERMINA GIRO dentro GiroAttivo."""
    import json as _json
    meta = {
        "tipo": "STATO_GIRO",
        "giro_terminato": bool(st.session_state.get("giro_terminato", False)),
        "inizio_giro_reale": st.session_state.get("inizio_giro_reale"),
        "fine_giro_reale": st.session_state.get("fine_giro_reale"),
        "previsione_giro": st.session_state.get("previsione_giro"),
        "fermo_mezzo_attivo": bool(st.session_state.get("fermo_mezzo_attivo", False)),
        "inizio_fermo_mezzo": st.session_state.get("inizio_fermo_mezzo"),
        "minuti_fermo_mezzo": float(st.session_state.get("minuti_fermo_mezzo", 0) or 0),
    }
    payload = _json.dumps(_json_sicuro(meta), ensure_ascii=False, allow_nan=False)
    cols_ordine = ['UTENTE', 'POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta', 'COLLI_CONSEGNATI', 'COLLI_RIFIUTATI', 'COLLI_DA_RENDERE', 'STATO', 'MIN_TRATTA_PREVISTA', 'MIN_PREVISTI_CUMULATIVI', 'TIPO_RIGA', 'BACKUP_JSON']
    for tentativo in range(5):
        try:
            if sheet_giro:
                time.sleep(1.5 * (tentativo + 1))
                data_totale = sheet_giro.get_all_records()
                df_tutti = pd.DataFrame(data_totale) if data_totale else pd.DataFrame(columns=cols_ordine)
                if not df_tutti.empty:
                    df_tutti.columns = df_tutti.columns.str.strip().str.upper()
                    if 'Q.TA' in df_tutti.columns:
                        df_tutti = df_tutti.rename(columns={'Q.TA': 'Q.ta'})
                    for c in cols_ordine:
                        if c not in df_tutti.columns:
                            df_tutti[c] = ""
                    df_tutti = df_tutti[cols_ordine]
                    meta_user = _meta_utente_giro(nome_utente).strip().lower()
                    mask_meta = df_tutti['UTENTE'].astype(str).str.strip().str.lower() == meta_user
                    df_tutti = df_tutti.loc[~mask_meta].copy()
                riga = {c: "" for c in cols_ordine}
                riga['UTENTE'] = _meta_utente_giro(nome_utente)
                riga['TIPO_RIGA'] = 'STATO_GIRO'
                riga['BACKUP_JSON'] = payload
                df_tutti = pd.concat([df_tutti, pd.DataFrame([riga])], ignore_index=True)
                _scrivi_giro_su_sheets_sicuro(df_tutti, cols_ordine)
                st.cache_data.clear()
                return True
        except Exception:
            if tentativo == 4:
                return False
    return False

def salva_stato_consegna(idx, stato, colli_consegnati=None):
    """Aggiorna stato e consuntivo colli della consegna."""
    df = _assicura_colonne_colli(st.session_state.giro_corrente.copy())
    if df.empty or idx < 0 or idx >= len(df):
        return
    try:
        qta = int(round(float(df.at[idx, "Q.ta"]))) if pd.notna(df.at[idx, "Q.ta"]) else 0
    except Exception:
        qta = 0
    qta = max(0, qta)
    if stato == STATO_FATTO:
        consegnati, rifiutati = qta, 0
    elif stato == STATO_RESPINTO:
        consegnati, rifiutati = 0, qta
    elif stato == STATO_PARZIALE:
        consegnati = max(0, min(qta, int(colli_consegnati or 0)))
        rifiutati = qta - consegnati
    else:
        consegnati, rifiutati = 0, 0
    df.at[idx, 'STATO'] = stato
    df.at[idx, 'COLLI_CONSEGNATI'] = float(consegnati)
    df.at[idx, 'COLLI_RIFIUTATI'] = float(rifiutati)
    df.at[idx, 'COLLI_DA_RENDERE'] = float(rifiutati)
    st.session_state.giro_corrente = df
    st.session_state.fine_giro_reale = None
    st.session_state.giro_terminato = False
    salva_stato_giro_persistente(st.session_state.utente_corrente)
    salva_giro_utente_su_sheets(st.session_state.utente_corrente, df)

def avvia_fermo_mezzo():
    """Avvia un fermo operativo; il tempo resta escluso dal ritardo effettivo."""
    if not st.session_state.get("fermo_mezzo_attivo", False):
        st.session_state.fermo_mezzo_attivo = True
        st.session_state.inizio_fermo_mezzo = time.time()
        salva_stato_giro_persistente(st.session_state.utente_corrente)

def termina_fermo_mezzo():
    """Chiude il fermo e accumula i minuti di pausa."""
    if st.session_state.get("fermo_mezzo_attivo", False):
        inizio = st.session_state.get("inizio_fermo_mezzo")
        if inizio is not None:
            try:
                st.session_state.minuti_fermo_mezzo = float(st.session_state.get("minuti_fermo_mezzo", 0) or 0) + max(0.0, (time.time() - float(inizio)) / 60.0)
            except Exception:
                pass
        st.session_state.fermo_mezzo_attivo = False
        st.session_state.inizio_fermo_mezzo = None
        salva_stato_giro_persistente(st.session_state.utente_corrente)

def prepara_vista_giro(df):
    """Restituisce una vista operativa con i clienti da fare prima e quelli gestiti in fondo.
    Non modifica l'ordine reale salvato del giro: e' solo una vista grafica.
    """
    if df is None or df.empty:
        return df.copy() if df is not None else pd.DataFrame()
    out = df.copy().reset_index(drop=True)
    if "STATO" not in out.columns:
        out["STATO"] = STATO_DA_FARE
    out["STATO"] = out["STATO"].fillna("").astype(str)
    out["__IDX_ORIGINALE"] = list(range(len(out)))
    completati = out["STATO"].isin([STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO])
    return pd.concat([out.loc[~completati], out.loc[completati]], ignore_index=True)

def indirizzo_partenza_giro(df):
    """Ultimo cliente gestito; se non esiste, usa il deposito."""
    if df is not None and not df.empty and "STATO" in df.columns:
        gestiti = df[df["STATO"].fillna("").astype(str).isin([STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO])]
        if not gestiti.empty:
            row = gestiti.iloc[-1]
            return f"{row['VIA']}, {row['COMUNE']}"
    return DEPOSITO_VANGO

def indirizzi_per_percorso_giro(df):
    """Costruisce il percorso Maps senza clienti gia' gestiti."""
    if df is None or df.empty:
        return []
    pending = df[~df["STATO"].fillna("").astype(str).isin([STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO])].copy()
    return [f"{r['VIA']}, {r['COMUNE']}" for _, r in pending.iterrows()]

def sposta_cliente_pendente_nella_posizione(idx_reale, nuova_posizione):
    """Sposta un cliente ancora da consegnare nella posizione indicata tra i soli pendenti.

    I clienti gia' gestiti (FATTO/PARZIALE/RESPINTO) restano in fondo e mantengono
    il loro ordine. La posizione scelta dall'utente si riferisce quindi SOLO ai
    clienti ancora da consegnare.
    """
    df = st.session_state.giro_corrente.copy().reset_index(drop=True)
    if df.empty or idx_reale < 0 or idx_reale >= len(df):
        return False

    if "STATO" not in df.columns:
        df["STATO"] = STATO_DA_FARE

    stato = str(df.iloc[idx_reale].get("STATO", "")).strip()
    stati_gestiti = [STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO]
    if stato in stati_gestiti:
        return False

    pending_idx = [
        i for i in range(len(df))
        if str(df.iloc[i].get("STATO", "")).strip() not in stati_gestiti
    ]
    if idx_reale not in pending_idx:
        return False

    try:
        nuova_posizione = int(nuova_posizione)
    except Exception:
        return False
    nuova_posizione = max(1, min(nuova_posizione, len(pending_idx)))

    posizione_attuale = pending_idx.index(idx_reale) + 1
    if posizione_attuale == nuova_posizione:
        return False

    pending_ordinati = list(pending_idx)
    pending_ordinati.remove(idx_reale)
    pending_ordinati.insert(nuova_posizione - 1, idx_reale)

    gestiti_idx = [
        i for i in range(len(df))
        if str(df.iloc[i].get("STATO", "")).strip() in stati_gestiti
    ]

    nuovo_ordine_indici = pending_ordinati + gestiti_idx
    df_nuovo = df.iloc[nuovo_ordine_indici].reset_index(drop=True)
    df_nuovo["POSIZIONE"] = [str(i) for i in range(1, len(df_nuovo) + 1)]

    st.session_state.giro_corrente = df_nuovo
    st.session_state.metriche_giro_corrente = None
    st.session_state.metriche_giro_campo = None
    st.session_state.firma_metriche_giro_campo = None
    st.session_state.metriche_tempo_orari_corrente = None
    st.session_state.giro_ottimizzato_proposto = None
    st.session_state.metriche_ottimizzazione = None
    salva_giro_utente_su_sheets(st.session_state.utente_corrente, df_nuovo)
    return True


def elimina_cliente_dal_giro(idx):
    """Elimina una sola fermata dal giro corrente e aggiorna GiroAttivo.

    Il cliente resta nel database Foglio1: viene rimosso solo dal giro corrente.
    """
    df = st.session_state.giro_corrente.copy()
    if df.empty or idx < 0 or idx >= len(df):
        return
    cliente = str(df.iloc[idx].get("CLIENTE", "Cliente"))
    df = df.drop(df.index[idx]).reset_index(drop=True)
    df["POSIZIONE"] = [str(i) for i in range(1, len(df) + 1)]
    st.session_state.giro_corrente = df
    st.session_state.giro_ottimizzato_proposto = None
    st.session_state.metriche_ottimizzazione = None
    st.session_state.conferma_eliminazione_idx = None
    salva_giro_utente_su_sheets(st.session_state.utente_corrente, df)
    st.session_state.cliente_eliminato_messaggio = f"🗑️ {cliente} eliminato dal giro."
    st.rerun()

def _chiave_cliente_giro(row):
    """Chiave stabile per riconoscere una fermata senza usare POSIZIONE."""
    return (
        str(row.get('CLIENTE', '')).strip().casefold(),
        str(row.get('COMUNE', '')).strip().casefold(),
        str(row.get('VIA', '')).strip().casefold(),
        str(row.get('ORA', '')).strip().casefold(),
    )

def _crea_snapshot_ordine(df):
    """Memorizza una copia esatta delle righe del giro al momento del backup.

    Il backup riguarda esclusivamente il giro corrente in GiroAttivo: se dopo il
    salvataggio una fermata viene eliminata, il ripristino deve poterla ricreare.
    Non viene mai usato per modificare Foglio1 o Utenti.
    """
    df_snapshot = df.reset_index(drop=True).copy()
    # JSON non gestisce NaN/NaT in modo affidabile: li trasformiamo in stringa vuota.
    df_snapshot = df_snapshot.where(pd.notna(df_snapshot), "")
    righe = df_snapshot.to_dict(orient='records')
    snapshot = []
    for posizione, riga in enumerate(righe, start=1):
        riga_safe = {}
        for k, v in riga.items():
            if pd.isna(v):
                v = ""
            elif hasattr(v, "item"):
                try:
                    v = v.item()
                except Exception:
                    pass
            # Timestamp/datetime non sono necessari come oggetti nel backup:
            # li convertiamo in stringa ISO per rendere il JSON sempre valido.
            if isinstance(v, (pd.Timestamp, datetime, date)):
                v = v.isoformat()
            riga_safe[str(k)] = v
        riga_safe["__VANGO_POSIZIONE_BACKUP"] = posizione
        snapshot.append(riga_safe)
    return {
        "versione": 2,
        "tipo": "GIRO_COMPLETO",
        "righe": snapshot,
    }

def _trova_riga_snapshot(df, item, usati):
    chiave = tuple(item.get('chiave', []))
    occ = int(item.get('occorrenza', 0))
    candidati = [
        i for i, row in df.iterrows()
        if i not in usati and _chiave_cliente_giro(row) == chiave
    ]
    if 0 <= occ < len(candidati):
        return candidati[occ]
    return candidati[0] if candidati else None

def salva_posizione_giro():
    """Salva l'ordine corrente e il giro completo senza cancellare le fermate normali.

    Il backup viene scritto in una riga tecnica separata. La parte normale del
    GiroAttivo viene ricostruita a partire dal giro corrente, così un salvataggio
    della posizione non puo' trasformare GiroAttivo nel solo record BACKUP.
    """
    df = st.session_state.giro_corrente.copy().reset_index(drop=True)
    if df.empty or not st.session_state.utente_corrente:
        return False

    # Prima assicuriamo che le stime eventualmente mancanti siano aggiornate.
    try:
        _assicura_previsione_cumulativa_giro(salva=True)
        df = st.session_state.giro_corrente.copy().reset_index(drop=True)
    except Exception:
        pass

    snapshot = _crea_snapshot_ordine(df)
    payload = json.dumps(_json_sicuro(snapshot), ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    nome_utente = str(st.session_state.utente_corrente).strip()
    backup_utente = BACKUP_UTENTE_PREFIX + nome_utente

    cols_ordine = [
        'UTENTE', 'POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta',
        'COLLI_CONSEGNATI', 'COLLI_RIFIUTATI', 'COLLI_DA_RENDERE', 'STATO',
        'MIN_TRATTA_PREVISTA', 'MIN_PREVISTI_CUMULATIVI', 'TIPO_RIGA', 'BACKUP_JSON'
    ]

    for tentativo in range(5):
        try:
            if not sheet_giro:
                return False
            time.sleep(1.5 * (tentativo + 1))

            # Leggiamo SEMPRE il contenuto reale del foglio, non la cache.
            data = sheet_giro.get_all_records()
            df_all = pd.DataFrame(data) if data else pd.DataFrame(columns=cols_ordine)
            if not df_all.empty:
                df_all.columns = [str(c).strip() for c in df_all.columns]
                # Normalizza i nomi delle colonne senza perdere Q.ta.
                if 'Q.TA' in df_all.columns and 'Q.ta' not in df_all.columns:
                    df_all = df_all.rename(columns={'Q.TA': 'Q.ta'})
                for c in cols_ordine:
                    if c not in df_all.columns:
                        df_all[c] = ''
                df_all = df_all[cols_ordine]
            else:
                df_all = pd.DataFrame(columns=cols_ordine)

            # Togliamo solo le righe normali dell'utente corrente e il suo vecchio
            # backup. Gli altri utenti e le altre righe tecniche restano intatti.
            mask_utente = df_all['UTENTE'].astype(str).str.strip().str.lower() == nome_utente.lower()
            mask_backup = df_all['UTENTE'].astype(str).str.strip().str.lower() == backup_utente.lower()
            df_all = df_all.loc[~(mask_utente | mask_backup)].copy()

            # Reinseriamo SEMPRE il giro normale corrente.
            df_normale = df.copy()
            df_normale['UTENTE'] = nome_utente
            df_normale['POSIZIONE'] = [str(i) for i in range(1, len(df_normale) + 1)]
            for c in cols_ordine:
                if c not in df_normale.columns:
                    df_normale[c] = ''
            df_normale['TIPO_RIGA'] = ''
            df_normale['BACKUP_JSON'] = ''
            df_normale = df_normale[cols_ordine]

            # E poi la riga tecnica del backup completo.
            nuova = {c: '' for c in cols_ordine}
            nuova.update({
                'UTENTE': backup_utente,
                'POSIZIONE': nome_utente,
                'CLIENTE': 'BACKUP POSIZIONE GIRO',
                'TIPO_RIGA': 'BACKUP_POSIZIONE',
                'BACKUP_JSON': payload,
            })

            df_all = pd.concat(
                [df_all, df_normale, pd.DataFrame([nuova])],
                ignore_index=True
            )

            _scrivi_giro_su_sheets_sicuro(df_all, cols_ordine)
            st.cache_data.clear()
            st.session_state.giro_backup_disponibile = True
            return True
        except Exception as e:
            if "429" in str(e) and tentativo < 4:
                continue
            if tentativo == 4:
                st.error(f"❌ Impossibile salvare il backup del giro: {e}")
            break
    return False

def carica_snapshot_posizione():
    """Legge l'ultimo backup dell'utente da GiroAttivo."""
    if not sheet_giro or not st.session_state.utente_corrente:
        return None
    try:
        data = sheet_giro.get_all_records()
        if not data:
            return None
        df_all = pd.DataFrame(data)
        df_all.columns = [str(c).strip() for c in df_all.columns]
        backup_utente = BACKUP_UTENTE_PREFIX + str(st.session_state.utente_corrente).strip()
        righe = df_all[df_all.get('UTENTE', '').astype(str) == backup_utente] if 'UTENTE' in df_all.columns else pd.DataFrame()
        if righe.empty:
            return None
        payload = str(righe.iloc[-1].get('BACKUP_JSON', '') or '').strip()
        if not payload:
            return None
        return json.loads(payload)
    except Exception:
        return None

def ripristina_posizione_giro():
    """Ripristina ESATTAMENTE il giro memorizzato nel backup.

    A differenza della vecchia logica, il backup contiene anche le righe delle
    fermate. Quindi una fermata eliminata dopo il salvataggio viene ricreata.
    Il ripristino sostituisce il giro corrente con la fotografia salvata, senza
    aggiungere clienti presenti solo nel giro corrente.
    """
    snapshot = carica_snapshot_posizione()
    if not snapshot:
        return False

    # Nuovo formato: fotografia completa del giro al momento del salvataggio.
    if isinstance(snapshot, dict) and snapshot.get("tipo") == "GIRO_COMPLETO":
        righe = snapshot.get("righe", [])
        if not righe:
            return False
        try:
            df = pd.DataFrame(righe).copy()
            if "__VANGO_POSIZIONE_BACKUP" in df.columns:
                df = df.sort_values("__VANGO_POSIZIONE_BACKUP", kind="stable")
                df = df.drop(columns=["__VANGO_POSIZIONE_BACKUP"])
            # Ripristina esattamente l'ordine e la struttura delle righe salvate.
            df = df.reset_index(drop=True)
            if 'POSIZIONE' in df.columns:
                df['POSIZIONE'] = [str(i) for i in range(1, len(df) + 1)]
        except Exception:
            return False
    else:
        # Compatibilita' con eventuali vecchi backup V1 che memorizzavano solo l'ordine.
        df = st.session_state.giro_corrente.copy()
        if df.empty:
            return False
        usati = set()
        indici = []
        for item in snapshot:
            idx = _trova_riga_snapshot(df, item, usati)
            if idx is not None:
                indici.append(idx)
                usati.add(idx)
        if not indici:
            return False
        indici.extend([i for i in df.index if i not in usati])
        df = df.loc[indici].reset_index(drop=True)
        df['POSIZIONE'] = [str(i) for i in range(1, len(df) + 1)]

    st.session_state.giro_corrente = df
    st.session_state.metriche_giro_corrente = None
    st.session_state.giro_ottimizzato_proposto = None
    st.session_state.metriche_ottimizzazione = None
    salva_giro_utente_su_sheets(st.session_state.utente_corrente, df)
    return True


# Inizializzazione dati di sessione.
if 'autenticato' not in st.session_state:
    st.session_state.autenticato = False

if 'utente_corrente' not in st.session_state:
    st.session_state.utente_corrente = ""

if 'is_admin' not in st.session_state:
    st.session_state.is_admin = False

if 'pagina_attiva' not in st.session_state:
    st.session_state.pagina_attiva = "welcome"

if 'storage_letta' not in st.session_state:
    st.session_state.storage_letta = False

if 'ricordami_attivo' not in st.session_state:
    st.session_state.ricordami_attivo = False

if 'db_clienti' not in st.session_state:
    st.session_state.db_clienti = carica_db_da_google_sheets()

if 'utenti_sistema' not in st.session_state:
    st.session_state.utenti_sistema = carica_utenti_da_sheets()

# Stato della funzione elimina cliente: inizializzato PRIMA di qualsiasi accesso.
# Questo evita AttributeError al primo avvio dell'app.
if 'conferma_eliminazione_idx' not in st.session_state:
    st.session_state.conferma_eliminazione_idx = None

if 'cliente_eliminato_messaggio' not in st.session_state:
    st.session_state.cliente_eliminato_messaggio = None

if st.session_state.cliente_eliminato_messaggio:
    st.success(st.session_state.cliente_eliminato_messaggio)
    st.session_state.cliente_eliminato_messaggio = None

# Ripristina il login dal localStorage del singolo browser/dispositivo.
# Il componente browser è asincrono: al primo render può non aver ancora
# restituito il valore. Facciamo un solo rerun di inizializzazione.
if not st.session_state.autenticato:
    utente_persistente = leggi_sessione_persistente()

    if utente_persistente and utente_persistente in st.session_state.utenti_sistema:
        st.session_state.autenticato = True
        st.session_state.utente_corrente = utente_persistente
        st.session_state.is_admin = (utente_persistente.lower() == "admin")
        st.session_state.pagina_attiva = "giro"
        st.session_state.ricordami_attivo = True
        st.session_state.storage_letta = True

    elif not st.session_state.storage_letta:
        st.session_state.storage_letta = True
        time.sleep(0.5)
        st.rerun()

if 'giro_corrente' not in st.session_state or st.session_state.get('ultimo_utente_caricato') != st.session_state.utente_corrente:
    if st.session_state.utente_corrente:
        st.session_state.giro_corrente = carica_giro_utente_da_sheets(st.session_state.utente_corrente)
        st.session_state.giro_corrente = _assicura_colonne_colli(st.session_state.giro_corrente)
        stato_persistente = carica_stato_giro_persistente(st.session_state.utente_corrente)
        st.session_state.giro_terminato = bool(stato_persistente.get("giro_terminato", False))
        st.session_state.inizio_giro_reale = stato_persistente.get("inizio_giro_reale")
        st.session_state.fine_giro_reale = stato_persistente.get("fine_giro_reale")
        st.session_state.previsione_giro = stato_persistente.get("previsione_giro")
        st.session_state.fermo_mezzo_attivo = bool(stato_persistente.get("fermo_mezzo_attivo", False))
        st.session_state.inizio_fermo_mezzo = stato_persistente.get("inizio_fermo_mezzo")
        st.session_state.minuti_fermo_mezzo = float(stato_persistente.get("minuti_fermo_mezzo", 0) or 0)
        st.session_state.metriche_giro_corrente = None
        st.session_state.ultimo_utente_caricato = st.session_state.utente_corrente
    else:
        st.session_state.giro_corrente = pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta', 'COLLI_CONSEGNATI', 'COLLI_RIFIUTATI', 'COLLI_DA_RENDERE', 'STATO', 'MIN_TRATTA_PREVISTA', 'MIN_PREVISTI_CUMULATIVI'])
    st.session_state.metriche_giro_corrente = None

if 'clienti_selezionati_m' not in st.session_state:
    st.session_state.clienti_selezionati_m = []

if 'vista_pulita' not in st.session_state:
    st.session_state.vista_pulita = False
if 'vista_giro' not in st.session_state:
    st.session_state.vista_giro = 'PREPARAZIONE'
if 'previsione_giro' not in st.session_state:
    st.session_state.previsione_giro = None
if 'inizio_giro_reale' not in st.session_state:
    st.session_state.inizio_giro_reale = None
if 'fine_giro_reale' not in st.session_state:
    st.session_state.fine_giro_reale = None
if 'giro_terminato' not in st.session_state:
    st.session_state.giro_terminato = False
if 'fermo_mezzo_attivo' not in st.session_state:
    st.session_state.fermo_mezzo_attivo = False
if 'inizio_fermo_mezzo' not in st.session_state:
    st.session_state.inizio_fermo_mezzo = None
if 'minuti_fermo_mezzo' not in st.session_state:
    st.session_state.minuti_fermo_mezzo = 0.0
if 'campo_parziale_idx' not in st.session_state:
    st.session_state.campo_parziale_idx = None

# GPS live: stato della posizione dell'autista sul dispositivo corrente.
if 'gps_attivo' not in st.session_state:
    st.session_state.gps_attivo = False
if 'gps_latitudine' not in st.session_state:
    st.session_state.gps_latitudine = None
if 'gps_longitudine' not in st.session_state:
    st.session_state.gps_longitudine = None
if 'gps_accuracy' not in st.session_state:
    st.session_state.gps_accuracy = None
if 'gps_timestamp' not in st.session_state:
    st.session_state.gps_timestamp = None
if 'gps_errore' not in st.session_state:
    st.session_state.gps_errore = None
if 'gps_component_counter' not in st.session_state:
    st.session_state.gps_component_counter = 0


def _toggle_gps():
    """Attiva/disattiva il GPS LIVE dal comando presente in tutte le viste."""
    if st.session_state.get("gps_attivo", False):
        st.session_state.gps_attivo = False
        st.session_state.gps_errore = None
    else:
        st.session_state.gps_attivo = True
        st.session_state.gps_errore = None
    st.rerun()


def _mostra_comando_gps(key):
    """Mostra il comando GPS con stato ON/OFF senza duplicare la logica."""
    if st.session_state.get("gps_attivo", False):
        if st.button("📍  SPEGNI GPS", use_container_width=True, key=key):
            _toggle_gps()
    else:
        if st.button("📍  ATTIVA GPS", use_container_width=True, key=key):
            _toggle_gps()

if 'forza_gruppamento_zona' not in st.session_state:
    st.session_state.forza_gruppamento_zona = 50

if 'modalita_ottimizzazione' not in st.session_state:
    st.session_state.modalita_ottimizzazione = "⚖️ ZONE + ROUTE"

if 'giro_ottimizzato_proposto' not in st.session_state:
    st.session_state.giro_ottimizzato_proposto = None

if 'metriche_ottimizzazione' not in st.session_state:
    st.session_state.metriche_ottimizzazione = None

if 'metriche_giro_corrente' not in st.session_state:
    st.session_state.metriche_giro_corrente = None
if 'metriche_giro_campo' not in st.session_state:
    st.session_state.metriche_giro_campo = None
if 'firma_metriche_giro_campo' not in st.session_state:
    st.session_state.firma_metriche_giro_campo = None
if 'metriche_tempo_orari_corrente' not in st.session_state:
    st.session_state.metriche_tempo_orari_corrente = None

if 'giro_backup_disponibile' not in st.session_state:
    st.session_state.giro_backup_disponibile = False

# V10.4.1: ricostruisce la previsione cumulativa quando cambia l'ordine reale.
# Il cambio STATO non modifica la firma, quindi la consegna appena gestita
# viene confrontata con il valore gia' salvato senza una nuova chiamata OSRM.
if st.session_state.get("utente_corrente") and not st.session_state.get("giro_corrente", pd.DataFrame()).empty:
    try:
        _assicura_previsione_cumulativa_giro(salva=True)
    except Exception:
        pass

if "nav" in st.query_params and st.query_params["nav"] == "login":
    st.session_state.pagina_attiva = "login"
    st.query_params.clear()

# CSS Avanzato
st.markdown("""
<style>
    .stApp, body, html {
        background-color: #121212 !important;
        color: #FFFFFF !important;
    }
    header {visibility: hidden;}
    .stMainBlockContainer { padding: 0rem !important; max-width: 100% !important; }
    .block-container { padding-top: 0.5rem !important; padding-bottom: 1rem !important; max-width: 100% !important; }

    .campo-header {
        background: linear-gradient(135deg, #142A44 0%, #102033 100%);
        border: 1px solid #26384F; border-radius: 14px; padding: 13px 16px;
        min-height: 68px; box-sizing: border-box; margin-bottom: 8px;
        display:flex; align-items:center;
    }
    .campo-header-left { display:flex; align-items:center; gap:12px; }
    .campo-header-icon { font-size:30px; line-height:1; }
    .campo-header-title { color:#FFFFFF; font-size:22px; font-weight:800; line-height:1; }
    .campo-header-subtitle { color:#A9C4EA; font-size:12px; margin-top:4px; }

    .logo-container {
        display: flex;
        justify-content: center;
        align-items: center;
        margin-bottom: 10px;
    }
    .logo-container img {
        width: 140px !important;
        max-width: 100%;
        height: auto;
    }

    div[data-testid="stHorizontalBlock"] { gap: 0.5rem !important; margin-bottom: -0.5rem !important; }
    div[data-testid="column"] { margin-bottom: 0px !important; }

    [data-testid="stMetricLabel"] { color: #CCCCCC !important; font-size: 14px !important; font-weight: 600 !important; }
    [data-testid="stMetricValue"] { color: #FFFFFF !important; font-size: 28px !important; font-weight: bold !important; }

    div[data-testid="stButton"] > button {
        background-color: #1E293B !important;
        color: #FFFFFF !important;
        border: 1px solid #475569 !important;
        border-radius: 8px !important;
        font-weight: bold !important;
    }

    .btn-active div[data-testid="stButton"] > button {
        background-color: #2563EB !important;
        color: #FFFFFF !important;
        border: 2px solid #60A5FA !important;
        height: 46px !important;
        font-size: 14px !important;
    }

    .btn-inactive div[data-testid="stButton"] > button {
        background-color: #1E293B !important;
        color: #94A3B8 !important;
        border: 1px solid #334155 !important;
        height: 46px !important;
        font-size: 14px !important;
    }

    div[data-baseweb="select"] { background-color: #1E293B !important; border-radius: 8px !important; }
    div[data-baseweb="select"] > div { background-color: #1E293B !important; color: #FFFFFF !important; border: 1px solid #3B82F6 !important; border-radius: 8px !important; }

    .stop-card {
        background-color: #1E1E1E;
        border-left: 5px solid #2563EB;
        padding: 12px 14px;
        border-radius: 10px;
        margin-top: 10px;
        border: 1px solid #334155;
    }
    .stop-title { font-size: 17px; font-weight: bold; color: #FFFFFF; margin-bottom: 4px; }
    .stop-address { font-size: 14px; color: #E2E8F0; margin-bottom: 6px; }
    .stop-meta { font-size: 13px; color: #60A5FA; font-weight: 600; }

    .clean-card {
        background-color: #1E1E1E;
        border: 1px solid #334155;
        border-radius: 14px;
        padding: 10px 12px;
        margin-bottom: 10px;
        display: flex;
        align-items: center;
        gap: 16px;
    }
    .clean-badge {
        background-color: #DBEAFE;
        color: #1D4ED8;
        width: 36px;
        height: 36px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        font-weight: bold;
        font-size: 16px;
        flex-shrink: 0;
    }
    .clean-content { flex-grow: 1; }
    .clean-title { font-size: 16px; font-weight: bold; color: #FFFFFF; margin-bottom: 2px; }
    .clean-subtitle { font-size: 13px; color: #94A3B8; }

    /* Layout card riepilogo: nessun cestino. */
    div[data-testid="stVerticalBlockBorderWrapper"] div[data-testid="stHorizontalBlock"] > div:nth-child(3) {
        display: flex;
        align-items: flex-start;
        justify-content: flex-end;
        min-height: 0;
        padding-top: 0;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] div[data-testid="stHorizontalBlock"] > div:nth-child(3) div[data-testid="stButton"] {
        width: auto !important;
        margin: 0 !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] div[data-testid="stHorizontalBlock"] > div:nth-child(3) button {
        width: 42px !important;
        min-width: 42px !important;
        height: 42px !important;
        min-height: 42px !important;
        padding: 0 !important;
        margin: 0 !important;
        border: none !important;
        background: transparent !important;
        box-shadow: none !important;
        font-size: 18px !important;
        line-height: 42px !important;
        display: flex !important;
        align-items: center !important;
        justify-content: center !important;
    }
    div[data-testid="stVerticalBlockBorderWrapper"] div[data-testid="stHorizontalBlock"] > div:nth-child(3) button:hover {
        background: rgba(255,255,255,0.06) !important;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# SCHERMATA 0: WELCOME / HOME PAGE
# ==========================================
if not st.session_state.autenticato and st.session_state.pagina_attiva == "welcome":
    img_path = "vango_splash.png"
    if os.path.exists(img_path):
        with open(img_path, "rb") as image_file:
            encoded_string = base64.b64encode(image_file.read()).decode()
        
        st.markdown(f"""
        <style>
            .hero-fullscreen {{
                position: fixed;
                top: 0; left: 0;
                width: 100vw; height: 100vh;
                background-image: url("data:image/png;base64,{encoded_string}");
                background-size: cover;
                background-position: left center;
                background-repeat: no-repeat;
                z-index: 99999;
                display: flex;
                justify-content: center;
                align-items: flex-end;
            }}
            .hero-btn-overlay {{
                position: absolute;
                bottom: 6%; left: 50%;
                transform: translateX(-50%);
                background: rgba(18, 18, 18, 0.4) !important;
                backdrop-filter: blur(8px);
                -webkit-backdrop-filter: blur(8px);
                color: #FFFFFF !important;
                padding: 14px 20px;
                border-radius: 30px;
                font-weight: bold;
                text-decoration: none !important;
                text-align: center;
                width: 85%; max-width: 400px;
                font-size: 16px;
                border: 2px solid rgba(96, 165, 250, 0.8) !important;
                box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
                z-index: 100000;
                transition: all 0.3s ease;
            }}
            .hero-btn-overlay:hover {{
                background: rgba(37, 99, 235, 0.7) !important;
                border-color: #60A5FA !important;
                color: #FFFFFF !important;
            }}
        </style>
        <div class="hero-fullscreen">
            <a href="?nav=login" target="_self" class="hero-btn-overlay">ENTRA IN VanGo</a>
        </div>
        """, unsafe_allow_html=True)
    else:
        st.warning("⚠️ Immagine 'vango_splash.png' non trovata nella cartella.")
        if st.button("ENTRA IN VanGo", use_container_width=True, type="primary"):
            st.session_state.pagina_attiva = "login"
            st.rerun()

# ==========================================
# SCHERMATA DI LOGIN
# ==========================================
elif not st.session_state.autenticato and st.session_state.pagina_attiva == "login":
    st.markdown("<div style='margin-top: 40px;'></div>", unsafe_allow_html=True)
    
    icon_path = "icovg.png"
    if os.path.exists(icon_path):
        with open(icon_path, "rb") as icon_file:
            encoded_icon = base64.b64encode(icon_file.read()).decode()
        st.markdown(f'''
            <div class="logo-container">
                <img src="data:image/png;base64,{encoded_icon}" alt="VanGo Logo">
            </div>
        ''', unsafe_allow_html=True)
    else:
        st.markdown("<h1 style='text-align: center; color: #FFFFFF; font-size: 26px;'>🚐 ACCESSO VANGO</h1>", unsafe_allow_html=True)

    st.markdown("<p style='text-align: center; color: #94A3B8; font-size: 14px; margin-bottom: 30px;'>Inserisci le credenziali per accedere al sistema</p>", unsafe_allow_html=True)

    col_l1, col_l2, col_l3 = st.columns([1, 2, 1])
    with col_l2:
        with st.form("form_login"):
            username_input = st.text_input("Utente")
            password_input = st.text_input("Password", type="password")

            ricordami = st.checkbox(
                "☑️ Ricordami su questo dispositivo",
                value=True,
                help="Se attivo, resterai collegato su questo browser anche dopo aver chiuso e riaperto l'app."
            )
            
            st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
            submit_login = st.form_submit_button("ACCEDI", use_container_width=True, type="primary")

            if submit_login:
                st.session_state.utenti_sistema = carica_utenti_da_sheets()
                utenti_validi = st.session_state.utenti_sistema
                username_input = username_input.strip()

                if username_input in utenti_validi and utenti_validi[username_input] == password_input:
                    st.session_state.autenticato = True
                    st.session_state.utente_corrente = username_input
                    st.session_state.is_admin = (username_input.lower() == "admin")
                    st.session_state.pagina_attiva = "giro"


                    # Salva il login nel browser solo se "Ricordami" è selezionato.
                    if ricordami:
                        salva_sessione_persistente(username_input)
                        st.session_state.ricordami_attivo = True
                        # Il componente browser è asincrono: lasciamogli il tempo
                        # di scrivere il valore prima del rerun.
                        time.sleep(1.5)
                    else:
                        elimina_sessione_persistente()
                        st.session_state.ricordami_attivo = False
                    
                    st.session_state.giro_corrente = carica_giro_utente_da_sheets(username_input)
                    stato_persistente = carica_stato_giro_persistente(username_input)
                    st.session_state.giro_terminato = bool(stato_persistente.get("giro_terminato", False))
                    st.session_state.inizio_giro_reale = stato_persistente.get("inizio_giro_reale")
                    st.session_state.fine_giro_reale = stato_persistente.get("fine_giro_reale")
                    st.session_state.ultimo_utente_caricato = username_input
                    
                    st.rerun()
                else:
                    st.error("❌ Utente o password errati.")

        if st.button("⬅️ Torna alla Home", use_container_width=True):
            st.session_state.pagina_attiva = "welcome"
            st.rerun()

# ==========================================
# APPLICAZIONE PRINCIPALE (ACCESSO CONSENTITO)
# ==========================================
else:
    if st.session_state.vista_giro == "CAMPO":
        # Testata CAMPO compatta: nessun logo, utente o LOGOUT durante la guida.
        campo_h1, campo_h2 = st.columns([3.7, 1.3], gap="small")
        with campo_h1:
            st.markdown("""
            <div class="campo-header">
                <div class="campo-header-left">
                    <div class="campo-header-icon">📍</div>
                    <div>
                        <div class="campo-header-title">CAMPO</div>
                        <div class="campo-header-subtitle">Giro in corso - Consegne in lavorazione</div>
                    </div>
                </div>
            </div>
            """, unsafe_allow_html=True)
        with campo_h2:
            st.markdown('<div style="height:2px"></div>', unsafe_allow_html=True)
            _mostra_comando_gps("btn_gps_campo_header")
            st.markdown('<div style="height:5px"></div>', unsafe_allow_html=True)

            # Il GPS LIVE viene gestito nel riquadro GPS piu' sotto.
            # Qui lasciamo soltanto il comando ON/OFF, evitando di creare una
            # seconda istanza del componente getLocation().
            if st.button("↩️ TORNA A VISTA RIEPILOGO", use_container_width=True, key="btn_torna_riepilogo_campo"):
                st.session_state.vista_giro = "RIEPILOGO"
                st.rerun()
    else:
        icon_path = "icovg.png"
        if os.path.exists(icon_path):
            with open(icon_path, "rb") as icon_file:
                encoded_icon = base64.b64encode(icon_file.read()).decode()
            st.markdown(f'''
                <div class="logo-container">
                    <img src="data:image/png;base64,{encoded_icon}" alt="VanGo Logo">
                </div>
            ''', unsafe_allow_html=True)
        else:
            st.markdown("<h1 style='text-align: center; color: #FFFFFF; font-size: 22px; margin-bottom: 5px; margin-top: 0px;'>🚐 VANGO</h1>", unsafe_allow_html=True)

        col_info_u, col_logout_u = st.columns([3, 1])
        with col_info_u:
            st.markdown(f"<p style='color: #94A3B8; font-size: 13px; margin: 0;'>👤 {st.session_state.get('utente_corrente', '')} sta usando Vango ver. <b style='color: #60A5FA;'>{VERSIONE_VANGO}</b></p>", unsafe_allow_html=True)
        with col_logout_u:
            if st.button("🚪 LOGOUT", use_container_width=True, key="btn_logout_principale"):
                elimina_sessione_persistente()
                st.session_state.autenticato = False
                st.session_state.utente_corrente = ""
                st.session_state.is_admin = False
                st.session_state.pagina_attiva = "login"
                st.rerun()

    st.markdown("<div style='margin-bottom: 10px;'></div>", unsafe_allow_html=True)

    if st.session_state.vista_giro != "CAMPO":
        if st.session_state.is_admin:
            col_sw1, col_sw2, col_sw3, col_sw4 = st.columns(4)
        else:
            col_sw1, col_sw2, col_sw3 = st.columns(3)

        with col_sw1:
            css_class = "btn-active" if st.session_state.pagina_attiva == "db" else "btn-inactive"
            st.markdown(f'<div class="{css_class}">', unsafe_allow_html=True)
            if st.button("📁 CLIENTI", use_container_width=True, key="btn_db"):
                st.session_state.pagina_attiva = "db"
                st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)

        with col_sw2:
            st.markdown('<div class="btn-inactive">', unsafe_allow_html=True)
            _mostra_comando_gps("btn_gps_navigazione")
            st.markdown('</div>', unsafe_allow_html=True)

        with col_sw3:
            css_class = "btn-active" if st.session_state.pagina_attiva == "analisi" else "btn-inactive"
            st.markdown(f'<div class="{css_class}">', unsafe_allow_html=True)
            if st.button("📊 ANALISI", use_container_width=True, key="btn_analisi"):
                st.session_state.pagina_attiva = "analisi"
                st.session_state.vista_giro = "RIEPILOGO"
                st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)

        if st.session_state.is_admin:
            with col_sw4:
                css_class = "btn-active" if st.session_state.pagina_attiva == "utenti" else "btn-inactive"
                st.markdown(f'<div class="{css_class}">', unsafe_allow_html=True)
                if st.button("🔑 UTENTI", use_container_width=True, key="btn_utenti"):
                    st.session_state.pagina_attiva = "utenti"
                    st.rerun()
                st.markdown('</div>', unsafe_allow_html=True)



        col_act1, col_act2 = st.columns(2)

        with col_act1:
            st.markdown('<div class="btn-inactive">', unsafe_allow_html=True)
            if st.button("🔄 INVERTI SEQUENZA", use_container_width=True, key="btn_inverti"):
                if not st.session_state.giro_corrente.empty:
                    st.session_state.giro_corrente = st.session_state.giro_corrente.iloc[::-1].reset_index(drop=True)
                    st.session_state.metriche_giro_corrente = None
                    st.session_state.giro_corrente['POSIZIONE'] = [str(i) for i in range(1, len(st.session_state.giro_corrente) + 1)]
                    salva_giro_utente_su_sheets(st.session_state.utente_corrente, st.session_state.giro_corrente)
                    st.session_state.giro_ottimizzato_proposto = None
                    st.session_state.metriche_ottimizzazione = None
                    st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)

        with col_act2:
            st.markdown('<div class="btn-inactive">', unsafe_allow_html=True)
            if st.button("🗑️ SVUOTA GIRO", use_container_width=True, key="btn_svuota"):
                if not st.session_state.giro_corrente.empty:
                    st.session_state.giro_corrente = pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta', 'COLLI_CONSEGNATI', 'COLLI_RIFIUTATI', 'COLLI_DA_RENDERE', 'STATO', 'MIN_TRATTA_PREVISTA', 'MIN_PREVISTI_CUMULATIVI'])
                    st.session_state.giro_terminato = False
                    st.session_state.inizio_giro_reale = None
                    st.session_state.fine_giro_reale = None
                    st.session_state.metriche_giro_corrente = None
                    salva_stato_giro_persistente(st.session_state.utente_corrente)
                    salva_giro_utente_su_sheets(st.session_state.utente_corrente, st.session_state.giro_corrente)
                    st.session_state.giro_ottimizzato_proposto = None
                    st.session_state.metriche_ottimizzazione = None
                    st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)

        col_backup1, col_backup2 = st.columns(2)
        with col_backup1:
            st.markdown('<div class="btn-inactive">', unsafe_allow_html=True)
            if st.button("💾 SALVA POSIZIONE GIRO", use_container_width=True, key="btn_salva_posizione"):
                if st.session_state.giro_corrente.empty:
                    st.warning("⚠️ Il giro è vuoto: non c'è nulla da memorizzare.")
                elif salva_posizione_giro():
                    st.success("💾 Ordine attuale del giro memorizzato. Potrai ripristinarlo in qualsiasi momento.")
            st.markdown('</div>', unsafe_allow_html=True)

        with col_backup2:
            st.markdown('<div class="btn-inactive">', unsafe_allow_html=True)
            if st.button("↩️ RIPRISTINA GIRO SALVATO", use_container_width=True, key="btn_ripristina_posizione"):
                if ripristina_posizione_giro():
                    st.success("↩️ Giro riportato all'ordine memorizzato.")
                    st.rerun()
                else:
                    st.warning("⚠️ Nessun backup del giro disponibile per questo utente.")
            st.markdown('</div>', unsafe_allow_html=True)


        st.session_state.modalita_ottimizzazione = st.selectbox(
            "🧠 Modalità ottimizzazione",
            options=[
                "🛣️ ROUTE",
                "📍 ZONE",
                "⚖️ ZONE + ROUTE",
                "🕐 ORARI — TEST",
            ],
            index=[
                "🛣️ ROUTE",
                "📍 ZONE",
                "⚖️ ZONE + ROUTE",
                "🕐 ORARI — TEST",
            ].index(st.session_state.modalita_ottimizzazione)
            if st.session_state.modalita_ottimizzazione in [
                "🛣️ ROUTE",
                "📍 ZONE",
                "⚖️ ZONE + ROUTE",
                "🕐 ORARI — TEST",
            ] else 2,
            key="select_modalita_ottimizzazione",
        )

        if st.session_state.modalita_ottimizzazione == "🛣️ ROUTE":
            st.session_state.forza_gruppamento_zona = 0
            st.caption("🛣️ ROUTE — motore V9 attuale: ottimizzazione stradale pura, ZONA ignorata.")
        elif st.session_state.modalita_ottimizzazione == "📍 ZONE":
            st.session_state.forza_gruppamento_zona = 100
            st.caption("📍 ZONE — motore V9 attuale: ordine macro-ZONA crescente, clienti ottimizzati dentro ogni ZONA.")
        elif st.session_state.modalita_ottimizzazione == "⚖️ ZONE + ROUTE":
            st.session_state.forza_gruppamento_zona = 50
            st.caption("⚖️ ZONE + ROUTE — motore V9 attuale al 50%: compromesso strada + ZONA.")
        else:
            st.session_state.forza_gruppamento_zona = 0
            if st.session_state.get("inizio_giro_reale") is not None:
                partenza_orari_txt = _formatta_ora_partenza_reale()
            else:
                partenza_orari_txt = "non ancora avviato"
            st.caption(f"🕐 ORARI — TEST — strada + orari di apertura minima. 01:00 = orario sconosciuto, quindi nessun vincolo. Partenza reale: {partenza_orari_txt}.")

        if st.button("🧠 OTTIMIZZA GIRO", use_container_width=True, key="btn_ottimizza"):
            if st.session_state.giro_corrente.empty:
                st.warning("⚠️ Il giro è vuoto.")
            elif len(st.session_state.giro_corrente) < 2:
                st.info("ℹ️ Servono almeno 2 fermate per ottimizzare il giro.")
            else:
                try:
                    with st.spinner("🧠 Analizzo indirizzi, percorso stradale e vincoli..."):
                        if st.session_state.modalita_ottimizzazione == "🕐 ORARI — TEST":
                            ora_partenza_orari = _ora_partenza_reale_minuti()
                            if ora_partenza_orari is None:
                                try:
                                    tz = ZoneInfo("Europe/Rome") if ZoneInfo is not None else None
                                    adesso_orari = datetime.now(tz) if tz else datetime.now()
                                    ora_partenza_orari = adesso_orari.hour * 60 + adesso_orari.minute + adesso_orari.second / 60.0
                                except Exception:
                                    ora_partenza_orari = 0.0
                            df_opt, metriche_opt = ottimizza_giro_orari_test(
                                st.session_state.giro_corrente,
                                st.session_state.db_clienti,
                                ora_partenza_minuti=ora_partenza_orari,
                            )
                        else:
                            df_opt, metriche_opt = ottimizza_giro_free(
                                st.session_state.giro_corrente,
                                st.session_state.db_clienti,
                                forza_gruppamento_zona=st.session_state.forza_gruppamento_zona
                            )
                    coordinate_da_salvare = metriche_opt.pop("coordinate_da_salvare", {})
                    if coordinate_da_salvare:
                        st.session_state.db_clienti = _aggiorna_coordinate_db(
                            st.session_state.db_clienti,
                            st.session_state.giro_corrente,
                            coordinate_da_salvare
                        )
                        salva_coordinate_su_google_sheets(st.session_state.db_clienti)
                    st.session_state.giro_ottimizzato_proposto = df_opt
                    st.session_state.metriche_ottimizzazione = metriche_opt
                    st.success("Giro ottimizzato pronto: controllalo e poi scegli se applicarlo.")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ Ottimizzazione non riuscita: {e}")
    st.markdown('</div>', unsafe_allow_html=True)

    st.markdown("<div style='margin-bottom: 5px;'></div>", unsafe_allow_html=True)
    st.markdown("<div style='margin-bottom: 5px;'></div>", unsafe_allow_html=True)

    # ==========================================
    # ANTEPRIMA GIRO OTTIMIZZATO
    # ==========================================
    if st.session_state.giro_ottimizzato_proposto is not None:
        df_proposto = st.session_state.giro_ottimizzato_proposto
        m = st.session_state.metriche_ottimizzazione or {}
        st.markdown("---")
        st.subheader("🧠 Anteprima percorso ottimizzato")
        if str(m.get("metodo", "")).startswith("ORARI"):
            st.caption("Start e fine giro: Dolciaria Acquaviva — Via Enrico Fermi 10, Burago di Molgora. Partenza test alle 05:00. 01:00 = orario sconosciuto, quindi nessun vincolo.")
            st.info(f"🕐 Orari conosciuti: **{m.get('orari_conosciuti', 0)}** — sconosciuti (01:00/vuoti): **{m.get('orari_sconosciuti', 0)}** — attesa totale: **{m.get('attesa_totale_min', 0)} min**")
        else:
            st.caption("Start e fine giro: Dolciaria Acquaviva — Via Enrico Fermi 10, Burago di Molgora. Il campo ORA non viene usato per l'ottimizzazione V9.")
            st.info(f"🎯 Forza raggruppamento ZONA usata: **{m.get('forza_gruppamento_zona', st.session_state.forza_gruppamento_zona)}%**")
        seq_zona = m.get("sequenza_zona", [])
        if seq_zona:
            st.caption(f"🗺️ Sequenza ZONA: **{' → '.join(map(str, seq_zona))}**  |  Cambi ZONA: **{m.get('cambi_zona', 0)}**  |  Rientri: **{m.get('rientri_zona', 0)}**")
            st.caption(f"📦 Macro-ZONE trovate: **{m.get('gruppi_zona', 0)}** — Metodo scelto: **{m.get('metodo', '')}**")
            if m.get("forza_gruppamento_zona", 0) == 100 and seq_zona:
                st.caption("🔒 Al 100%: le macro-ZONE sono mantenute in ordine crescente e ogni ZONA viene completata prima della successiva.")
        elif m.get("gruppi_zona", 0) == 0:
            st.warning("⚠️ Nessuna ZONA disponibile per le fermate di questo giro: la percentuale non può influire sul percorso.")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Km", f"{m.get('km_ottimizzati', 0):.1f}", delta=f"{m.get('risparmio_km', 0):+.1f} km")
        c2.metric("Tempo strada", _formatta_durata_hm(m.get('min_ottimizzati', 0)), delta=f"{m.get('risparmio_min', 0):+.0f} min")
        c3.metric("Fermate", f"{m.get('fermate', len(df_proposto))}")
        c4.metric("Metodo", "FREE")

        # Per la modalità ORARI mostriamo subito sotto le metriche attuali
        # il dettaglio del tempo reale del giro.
        if str(m.get("metodo", "")).startswith("ORARI"):
            t_viaggio = m.get("min_ottimizzati", 0)
            t_attesa = m.get("attesa_totale_min", 0)
            t_servizio = m.get("servizio_totale_min", len(df_proposto) * MINUTI_SERVIZIO_PER_FERMATA)
            t_reale = m.get("tempo_totale_reale_min", t_viaggio + t_attesa + t_servizio)

            st.markdown("**Dettaglio tempi del giro ORARI**")
            d1, d2, d3, d4 = st.columns(4)
            d1.metric("🚚 Tempo di viaggio", _formatta_durata_hm(t_viaggio))
            d2.metric("⏳ Attesa totale", _formatta_durata_hm(t_attesa))
            d3.metric("🅿️ Servizio totale", _formatta_durata_hm(t_servizio))
            d4.metric("🕐 Tempo totale reale giro", _formatta_durata_hm(t_reale))

        colonne_anteprima = ['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']
        if str(m.get("metodo", "")).startswith("ORARI") and 'ARRIVO STIMATO' in df_proposto.columns:
            colonne_anteprima = ['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'ARRIVO STIMATO', 'Q.ta']
        st.dataframe(
            df_proposto[colonne_anteprima],
            hide_index=True,
            use_container_width=True,
        )

        col_applica, col_annulla = st.columns(2)
        with col_applica:
            if st.button("✅ APPLICA GIRO OTTIMIZZATO", use_container_width=True, type="primary", key="btn_applica_ottimizzato"):
                df_da_applicare = df_proposto.copy()
                if 'ARRIVO STIMATO' in df_da_applicare.columns:
                    df_da_applicare = df_da_applicare.drop(columns=['ARRIVO STIMATO'])
                # Le previsioni della proposta precedente non vanno riutilizzate:
                # vengono ricostruite sull'ordine appena applicato.
                # Prepara le nuove colonne come numeriche.
                # Google Sheets può restituirle come string/Arrow e pandas
                # non consente di inserire float dentro una colonna stringa.
                if 'MIN_TRATTA_PREVISTA' in df_da_applicare.columns:
                    df_da_applicare['MIN_TRATTA_PREVISTA'] = pd.to_numeric(
                        df_da_applicare['MIN_TRATTA_PREVISTA'], errors='coerce'
                    ).astype('float64')
                else:
                    df_da_applicare['MIN_TRATTA_PREVISTA'] = pd.Series(
                        float('nan'), index=df_da_applicare.index, dtype='float64'
                    )

                if 'MIN_PREVISTI_CUMULATIVI' in df_da_applicare.columns:
                    df_da_applicare['MIN_PREVISTI_CUMULATIVI'] = pd.to_numeric(
                        df_da_applicare['MIN_PREVISTI_CUMULATIVI'], errors='coerce'
                    ).astype('float64')
                else:
                    df_da_applicare['MIN_PREVISTI_CUMULATIVI'] = pd.Series(
                        float('nan'), index=df_da_applicare.index, dtype='float64'
                    )

                df_da_applicare['MIN_TRATTA_PREVISTA'] = float('nan')
                df_da_applicare['MIN_PREVISTI_CUMULATIVI'] = float('nan')
                st.session_state.giro_corrente = df_da_applicare
                st.session_state.metriche_giro_corrente = None
                # V10.2.9: conserva la previsione del tempo totale per il confronto finale.
                if str(m.get("metodo", "")).startswith("ORARI"):
                    tempo_previsto = float(m.get("tempo_totale_reale_min", 0) or 0)
                else:
                    tempo_previsto = float(m.get("min_ottimizzati", 0) or 0) + len(df_da_applicare) * MINUTI_SERVIZIO_PER_FERMATA
                st.session_state.previsione_giro = {
                    "minuti": tempo_previsto,
                    "metodo": str(m.get("metodo", "")),
                    "firma": _firma_ordine_giro(df_da_applicare),
                }
                df_previsto, totale_cumulativo = _calcola_previsione_cumulativa_giro(
                    df_da_applicare, st.session_state.db_clienti
                )
                if totale_cumulativo is not None:
                    st.session_state.giro_corrente = df_previsto
                    st.session_state.previsione_giro["minuti"] = float(totale_cumulativo)
                    st.session_state.previsione_giro["firma_ordine_cumulativa"] = _firma_ordine_giro(df_previsto)
                    df_da_applicare = df_previsto
                st.session_state.inizio_giro_reale = None
                st.session_state.fine_giro_reale = None
                st.session_state.giro_terminato = False
                st.session_state.fermo_mezzo_attivo = False
                st.session_state.inizio_fermo_mezzo = None
                st.session_state.minuti_fermo_mezzo = 0.0
                salva_stato_giro_persistente(st.session_state.utente_corrente)
                # Conserva i dati temporali ORARI del giro appena applicato.
                if str(m.get("metodo", "")).startswith("ORARI"):
                    st.session_state.metriche_tempo_orari_corrente = {
                        "firma": _firma_ordine_giro(df_da_applicare),
                        "attesa_totale_min": float(m.get("attesa_totale_min", 0) or 0),
                        "servizio_totale_min": float(m.get("servizio_totale_min", len(df_da_applicare) * MINUTI_SERVIZIO_PER_FERMATA) or 0),
                        "tempo_totale_reale_min": float(m.get("tempo_totale_reale_min", 0) or 0),
                    }
                else:
                    st.session_state.metriche_tempo_orari_corrente = None
                salva_giro_utente_su_sheets(st.session_state.utente_corrente, st.session_state.giro_corrente)
                st.session_state.giro_ottimizzato_proposto = None
                st.session_state.metriche_ottimizzazione = None
                st.success("✅ Giro ottimizzato salvato su Google Sheets.")
                st.rerun()
        with col_annulla:
            if st.button("❌ ANNULLA OTTIMIZZAZIONE", use_container_width=True, key="btn_annulla_ottimizzato"):
                st.session_state.giro_ottimizzato_proposto = None
                st.session_state.metriche_ottimizzazione = None
                st.rerun()

    # ==========================================
    # SCHERMATA 1: GIRO CONSEGNE
    # ==========================================
    if st.session_state.pagina_attiva == "giro":
        tot_clienti = len(st.session_state.giro_corrente)
        tot_qta = int(st.session_state.giro_corrente['Q.ta'].sum()) if not st.session_state.giro_corrente.empty else 0
        tot_comuni = int(st.session_state.giro_corrente['COMUNE'].nunique()) if not st.session_state.giro_corrente.empty else 0

        # Avanzamento del giro: immediato e visibile a colpo d'occhio.
        # I clienti FATTO/PARZIALE/RESPINTO sono considerati consegne gestite.
        if tot_clienti > 0:
            stati = st.session_state.giro_corrente.get("STATO", pd.Series([STATO_DA_FARE] * tot_clienti)).fillna("").astype(str)
            completate = int(stati.isin([STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO]).sum())
            percentuale = completate / tot_clienti
            st.markdown(f"""
            <div style="border:1px solid rgba(148,163,184,0.28); border-radius:12px; padding:12px 14px 10px 14px; margin:4px 0 14px 0; background:rgba(30,41,59,0.22);">
                <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:7px;">
                    <span style="font-size:15px; font-weight:700;">🚚 AVANZAMENTO GIRO</span>
                    <span style="font-size:16px; font-weight:800;">{completate} / {tot_clienti}</span>
                </div>
                <div style="height:10px; border-radius:999px; background:rgba(148,163,184,0.18); overflow:hidden;">
                    <div style="height:100%; width:{percentuale * 100:.1f}%; border-radius:999px; background:#22c55e;"></div>
                </div>
                <div style="font-size:12px; color:#94A3B8; margin-top:6px;">{completate} consegne completate su {tot_clienti}</div>
            </div>
            """, unsafe_allow_html=True)

        # Quando il giro e' completato, la vista CAMPO non deve piu' mostrare clienti.
        # Calcoliamo qui il flag prima di usarlo, cosi' non puo' verificarsi un NameError.
        stati_completamento = st.session_state.giro_corrente.get(
            "STATO",
            pd.Series([STATO_DA_FARE] * tot_clienti)
        ).fillna("").astype(str)
        tutte_gestite = (
            bool(tot_clienti)
            and int(stati_completamento.isin([STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO]).sum()) == tot_clienti
        )
        # Portiamo automaticamente l'utente sul RIEPILOGO, dove puo' vedere l'intera
        # progressione: prima i da fare e in fondo tutti i gestiti/offuscati.
        # A giro completato NON cambiamo automaticamente vista.
        # Se l'utente e' in CAMPO, la CAMPO resta vuota: mostra solo clienti da fare.
        # Il RIEPILOGO resta disponibile tramite il relativo pulsante e mostra tutti
        # i clienti, con quelli gestiti in fondo e leggermente offuscati.

        # V10.4.0 TEST: caricamento giro serale da foto (isolato, non tocca l'ottimizzatore).
        if st.session_state.vista_giro != "CAMPO":
            render_carica_giro_da_foto()

        # V10.3.2: in CAMPO le tre tab PREPARAZIONE/RIEPILOGO/CAMPO
        # non vengono mostrate: il ritorno al RIEPILOGO avviene dal comando dedicato.
        if not st.session_state.giro_corrente.empty and st.session_state.vista_giro != "CAMPO":
            v1, v2, v3 = st.columns(3)
            with v1:
                if st.button("🛠️ PREPARAZIONE", use_container_width=True, type="primary" if st.session_state.vista_giro == "PREPARAZIONE" else "secondary", key="vista_preparazione"):
                    st.session_state.vista_giro = "PREPARAZIONE"
                    st.rerun()
            with v2:
                if st.button("📋 RIEPILOGO", use_container_width=True, type="primary" if st.session_state.vista_giro == "RIEPILOGO" else "secondary", key="vista_riepilogo"):
                    st.session_state.vista_giro = "RIEPILOGO"
                    st.rerun()
            with v3:
                if st.button("🚚 CAMPO", use_container_width=True, type="primary" if st.session_state.vista_giro == "CAMPO" else "secondary", key="vista_campo"):
                    st.session_state.vista_giro = "CAMPO"
                    st.rerun()
            st.markdown("<div style='margin-bottom: 10px;'></div>", unsafe_allow_html=True)
        # Resoconto scaricabile: un solo tasto, sempre aggiornato con lo stato corrente.
        if st.session_state.pagina_attiva == "giro" and not st.session_state.giro_corrente.empty and st.session_state.vista_giro != "CAMPO":
                # Esporta il resoconto del giro in Excel.
                # Il file viene rigenerato ad ogni aggiornamento della pagina, quindi
                # contiene sempre lo stato consegna piu' recente senza usare un secondo tasto.
                try:
                    from openpyxl import Workbook
                    from openpyxl.styles import Font, Alignment, PatternFill, Border, Side
                    from openpyxl.utils import get_column_letter

                    df_export = prepara_vista_giro(st.session_state.giro_corrente).copy()
                    righe_export = []
                    for i, (_, r) in enumerate(df_export.iterrows(), start=1):
                        stato = str(r.get("STATO", "")).strip() or STATO_DA_FARE
                        if stato not in STATI_CONSEGNA:
                            stato = STATO_DA_FARE
                        righe_export.append({
                            "N°": i,
                            "CLIENTE": str(r.get("CLIENTE", "")).strip(),
                            "STATO CONSEGNA": stato,
                            # I dati provenienti da Google Sheets/OCR possono contenere
                            # NaN. Non usare "or 0" per i float: NaN e' truthy e int(NaN)
                            # genera "cannot convert float NaN to integer".
                            "COLLI PREVISTI": _intero_sicuro(r.get("Q.ta", 0)),
                            "COLLI CONSEGNATI": _intero_sicuro(r.get("COLLI_CONSEGNATI", 0)),
                            "COLLI RIFIUTATI": _intero_sicuro(r.get("COLLI_RIFIUTATI", 0)),
                            "COLLI DA RENDERE": _intero_sicuro(r.get("COLLI_DA_RENDERE", 0)),
                        })

                    wb = Workbook()
                    ws = wb.active
                    ws.title = "Resoconto Giro"
                    intestazioni = ["N°", "CLIENTE", "STATO CONSEGNA", "COLLI PREVISTI", "COLLI CONSEGNATI", "COLLI RIFIUTATI", "COLLI DA RENDERE"]
                    ws.append(intestazioni)

                    for riga in righe_export:
                        ws.append([riga[col] for col in intestazioni])

                    # Formattazione semplice e leggibile, pensata per l'uso quotidiano.
                    for cella in ws[1]:
                        cella.font = Font(bold=True)
                        cella.alignment = Alignment(horizontal="center", vertical="center")
                    ws.freeze_panes = "A2"
                    ws.auto_filter.ref = ws.dimensions
                    ws.row_dimensions[1].height = 24

                    larghezze = {"A": 8, "B": 42, "C": 24, "D": 16, "E": 18, "F": 17, "G": 17}
                    for col, larghezza in larghezze.items():
                        ws.column_dimensions[col].width = larghezza

                    bordo = Side(style="thin")
                    for row in ws.iter_rows():
                        for cella in row:
                            cella.border = Border(bottom=bordo)
                            cella.alignment = Alignment(vertical="center")

                    # Colori automatici solo sulla colonna dello stato.
                    for row in ws.iter_rows(min_row=2, min_col=3, max_col=3):
                        cella = row[0]
                        if cella.value == STATO_FATTO:
                            cella.fill = PatternFill("solid", fgColor="C6EFCE")
                        elif cella.value == STATO_PARZIALE:
                            cella.fill = PatternFill("solid", fgColor="FFEB9C")
                        elif cella.value == STATO_RESPINTO:
                            cella.fill = PatternFill("solid", fgColor="FFC7CE")
                        else:
                            cella.fill = PatternFill("solid", fgColor="E7E6E6")

                    buffer_excel = BytesIO()
                    wb.save(buffer_excel)
                    buffer_excel.seek(0)
                    nome_resoconto = f"resoconto_giro_{st.session_state.utente_corrente}.xlsx"
                    st.download_button(
                        "📊 ESPORTA RESOCONTO GIRO (EXCEL)",
                        data=buffer_excel.getvalue(),
                        file_name=nome_resoconto,
                        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        use_container_width=True,
                        key="btn_esporta_resoconto_giro"
                    )
                except Exception as e:
                    st.error(f"❌ Impossibile preparare il resoconto Excel: {e}")
                st.markdown("<div style='margin-bottom: 12px;'></div>", unsafe_allow_html=True)
        

        # KM e tempo: PREPARAZIONE/RIEPILOGO mostrano sempre il giro completo.
        # CAMPO mostra invece solo la parte ancora da fare, partendo dall'ultima
        # consegna gia' gestita (o dal deposito se non ne esiste una) e rientrando
        # al deposito. Nessuna riottimizzazione automatica.
        if st.session_state.vista_giro == "CAMPO":
            df_per_metriche = st.session_state.giro_corrente.copy().reset_index(drop=True)
            if "STATO" not in df_per_metriche.columns:
                df_per_metriche["STATO"] = STATO_DA_FARE
            stati_gestiti_metriche = [STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO]
            df_per_metriche = df_per_metriche[
                ~df_per_metriche["STATO"].fillna("").astype(str).isin(stati_gestiti_metriche)
            ].reset_index(drop=True)
            firma_campo = _firma_ordine_giro(df_per_metriche)
            if (
                st.session_state.get("firma_metriche_giro_campo") != firma_campo
                or st.session_state.get("metriche_giro_campo") is None
            ):
                try:
                    st.session_state.metriche_giro_campo = calcola_metriche_giro_campo(
                        st.session_state.giro_corrente,
                        st.session_state.db_clienti
                    )
                except Exception:
                    st.session_state.metriche_giro_campo = None
                st.session_state.firma_metriche_giro_campo = firma_campo
            metriche_giro = st.session_state.metriche_giro_campo or {}
        else:
            if st.session_state.metriche_giro_corrente is None and not st.session_state.giro_corrente.empty:
                try:
                    st.session_state.metriche_giro_corrente = calcola_metriche_giro_corrente(
                        st.session_state.giro_corrente,
                        st.session_state.db_clienti
                    )
                except Exception:
                    # Nessun errore bloccante nell'interfaccia: se OSRM non risponde
                    # o manca una coordinata, lasciamo semplicemente il valore "—".
                    st.session_state.metriche_giro_corrente = None
            metriche_giro = st.session_state.metriche_giro_corrente or {}
        km_giro = metriche_giro.get("km")
        minuti_giro = metriche_giro.get("minuti")

        # In CAMPO le metriche sono sempre quelle del percorso ancora da fare:
        # fermate pendenti + rientro in sede. A consegne completate resta quindi
        # visibile solo il rientro ultima consegna -> sede, finche' non si preme
        # TERMINA GIRO; dopo TERMINA GIRO il residuo diventa 0.
        km_visualizzati = km_giro
        minuti_visualizzati = minuti_giro
        if st.session_state.vista_giro == "CAMPO":
            if bool(st.session_state.get("giro_terminato", False)):
                km_visualizzati = 0.0
                minuti_visualizzati = 0.0
            else:
                try:
                    metriche_campo = calcola_metriche_giro_campo(
                        st.session_state.giro_corrente,
                        st.session_state.db_clienti
                    )
                    if metriche_campo is not None:
                        km_visualizzati = metriche_campo.get("km")
                        minuti_visualizzati = metriche_campo.get("minuti")
                except Exception:
                    pass

        if st.session_state.vista_giro != "CAMPO":
            col_m1, col_m2, col_m3, col_m4, col_m5 = st.columns(5)
            col_m1.metric("Fermate Totali", f"{tot_clienti}")
            col_m2.metric("Pezzi Totali", f"{tot_qta}")
            col_m3.metric("Comuni", f"{tot_comuni}")
            col_m4.metric("KM Totali", f"{km_visualizzati:.1f}" if km_visualizzati is not None else "—")
            if minuti_visualizzati is not None:
                tempo_display = _formatta_durata_hm(minuti_visualizzati)
            else:
                tempo_display = "—"
            col_m5.metric("Tempo Giro", tempo_display)

        # Dettaglio tempi del giro: SEMPRE visibile quando esiste un giro,
        # indipendentemente dal tipo di ottimizzazione o dal fatto che sia stato
        # ottimizzato. Il tempo di viaggio e' gia' mostrato sopra come "Tempo Giro".
        #
        # Attesa: per un giro normale/manuale e per ROUTE/ZONE/ZONE+ROUTE = 0.
        # Per un giro ORARI appena applicato usiamo l'attesa calcolata dal motore.
        # Servizio: sempre 12 minuti per fermata.
        # Tempo reale: viaggio + attesa + servizio.
        metriche_orari_correnti = st.session_state.get("metriche_tempo_orari_corrente") or {}
        firma_corrente = _firma_ordine_giro(st.session_state.giro_corrente)
        metriche_orari_valide = (
            bool(metriche_orari_correnti)
            and metriche_orari_correnti.get("firma") == firma_corrente
        )

        attesa_corrente = (
            float(metriche_orari_correnti.get("attesa_totale_min", 0) or 0)
            if metriche_orari_valide else 0.0
        )
        if st.session_state.vista_giro == "CAMPO":
            df_servizio = st.session_state.giro_corrente.copy()
            if "STATO" not in df_servizio.columns:
                df_servizio["STATO"] = STATO_DA_FARE
            df_servizio = df_servizio[
                ~df_servizio["STATO"].fillna("").astype(str).isin([STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO])
            ]
            servizio_corrente = float(len(df_servizio) * MINUTI_SERVIZIO_PER_FERMATA)
        else:
            servizio_corrente = float(len(st.session_state.giro_corrente) * MINUTI_SERVIZIO_PER_FERMATA)
        viaggio_corrente = float(minuti_visualizzati or 0)
        # In CAMPO il tempo totale residuo rappresenta il lavoro che resta:
        # strada + servizio delle sole consegne pendenti + eventuali attese residue.
        if st.session_state.vista_giro == "CAMPO":
            previsione_residua = st.session_state.get("previsione_giro") or {}
            previsto_totale = previsione_residua.get("minuti")
            residuo_da_previsione = None
            if previsto_totale is not None:
                try:
                    df_tmp = _assicura_colonne_colli(st.session_state.giro_corrente.copy())
                    stati_tmp = df_tmp["STATO"].fillna("").astype(str)
                    gestiti_tmp = df_tmp[stati_tmp.isin([STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO])]
                    if gestiti_tmp.empty:
                        residuo_da_previsione = float(previsto_totale)
                    else:
                        ultimo_cum = _numero_minuti_cumulativi(gestiti_tmp.iloc[-1].get("MIN_PREVISTI_CUMULATIVI"))
                        if ultimo_cum is not None:
                            # Il cumulativo si ferma all'arrivo: il servizio del cliente
                            # appena completato e' gia' stato svolto e quindi va escluso.
                            residuo_da_previsione = max(0.0, float(previsto_totale) - float(ultimo_cum) - float(MINUTI_SERVIZIO_PER_FERMATA))
                except Exception:
                    residuo_da_previsione = None
            tempo_reale_corrente = residuo_da_previsione if residuo_da_previsione is not None else (viaggio_corrente + attesa_corrente + servizio_corrente)
        else:
            tempo_reale_corrente = viaggio_corrente + attesa_corrente + servizio_corrente

        # In CAMPO la metrica "TEMPO RESIDUO" deve rappresentare il lavoro reale
        # ancora necessario, non il solo tempo di strada. Usa quindi il residuo
        # della previsione cumulativa: strada + servizio residuo + eventuali attese
        # + rientro in sede. Il motore di previsione non viene modificato.
        if st.session_state.vista_giro == "CAMPO":
            minuti_visualizzati = tempo_reale_corrente

        if st.session_state.vista_giro != "CAMPO":
            st.markdown("**Dettaglio tempi reali del giro**")
            d1, d2, d3 = st.columns(3)
            d1.metric("⏳ Attesa totale", _formatta_durata_hm(attesa_corrente))
            d2.metric("🅿️ Servizio totale", _formatta_durata_hm(servizio_corrente))
            d3.metric("🕐 Tempo totale reale giro", _formatta_durata_hm(tempo_reale_corrente))

            st.markdown("---")

        # V10.2.17: le consegne possono essere tutte gestite, ma il giro non e'
        # realmente terminato finche' il mezzo non rientra in sede e l'utente
        # preme TERMINA GIRO.
        stati_fine = st.session_state.giro_corrente.get("STATO", pd.Series([STATO_DA_FARE] * tot_clienti)).fillna("").astype(str) if tot_clienti else pd.Series(dtype=str)
        tutte_gestite = bool(tot_clienti) and int(stati_fine.isin([STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO]).sum()) == tot_clienti
        if tutte_gestite:
            giro_terminato = bool(st.session_state.get("giro_terminato", False))
            if giro_terminato:
                titolo_fine = "🏁 GIRO COMPLETATO"
                sottotitolo_fine = "Tutte le consegne sono state gestite e il rientro in sede e' stato registrato."
            else:
                titolo_fine = "📦 CONSEGNE COMPLETATE"
                sottotitolo_fine = "Tutte le consegne sono state gestite. Rientra in sede e premi TERMINA GIRO."
            st.markdown(f"""
            <div style='text-align:center; padding:22px 12px 12px 12px; margin:10px 0 14px 0; border:1px solid rgba(34,197,94,0.35); border-radius:16px; background:rgba(34,197,94,0.08);'>
                <div style='font-size:32px; font-weight:800;'>{titolo_fine}</div>
                <div style='font-size:14px; color:#94A3B8; margin-top:5px;'>{sottotitolo_fine}</div>
            </div>
            """, unsafe_allow_html=True)
            previsione = st.session_state.get("previsione_giro") or {}
            previsto = previsione.get("minuti")
            inizio = st.session_state.get("inizio_giro_reale")
            fine = st.session_state.get("fine_giro_reale")

            # La previsione finale deve essere il "Tempo totale reale giro" del giro
            # completo: tempo strada + attesa + 12 minuti per fermata.
            # Se non e' stata salvata dall'ottimizzatore, la ricaviamo qui dal giro
            # completo, senza usare il percorso residuo della CAMPO.
            if previsto is None and not st.session_state.giro_corrente.empty:
                try:
                    metriche_completo = calcola_metriche_giro_corrente(
                        st.session_state.giro_corrente,
                        st.session_state.db_clienti
                    )
                    if metriche_completo is not None:
                        previsto = float(metriche_completo.get("minuti", 0) or 0) + len(st.session_state.giro_corrente) * MINUTI_SERVIZIO_PER_FERMATA
                        # Per ORARI, se abbiamo gia' l'attesa calcolata, includila.
                        if metriche_orari_valide:
                            previsto += float(metriche_orari_correnti.get("attesa_totale_min", 0) or 0)
                        st.session_state.previsione_giro = {
                            "minuti": previsto,
                            "metodo": str(previsione.get("metodo", "TEMPO TOTALE GIRO")),
                            "firma": _firma_ordine_giro(st.session_state.giro_corrente),
                        }
                        salva_stato_giro_persistente(st.session_state.utente_corrente)
                except Exception:
                    previsto = previsto

            # Il confronto finale esiste solo se il giro e' stato realmente
            # avviato con INIZIA GIRO e poi terminato con TERMINA GIRO.
            if giro_terminato and previsto is not None and inizio is not None:
                if fine is not None:
                    effettivo = max(0.0, (float(fine) - float(inizio)) / 60.0)
                    differenza = float(effettivo) - float(previsto)
                    if differenza <= 0:
                        esito = f"🟢 {_formatta_durata_hm(abs(differenza))} risparmiati rispetto alla stima"
                    else:
                        esito = f"🔴 {_formatta_durata_hm(differenza)} in più rispetto alla stima"
                    st.markdown("**📊 CONFRONTO FINALE**")
                    a, b = st.columns(2)
                    a.metric("⏱️ Tempo previsto", _formatta_durata_hm(previsto))
                    b.metric("🚚 Tempo effettivo", _formatta_durata_hm(effettivo))
                    st.markdown(f"<div style='text-align:center; font-size:20px; font-weight:800; margin:8px 0 14px 0;'>{esito}</div>", unsafe_allow_html=True)
                else:
                    st.info("Tempo effettivo non disponibile.")
            elif not giro_terminato and previsto is not None:
                st.info("La stima verrà confrontata con il tempo effettivo quando premi TERMINA GIRO, dopo aver avviato il giro con INIZIA GIRO.")

            # Il rientro e' gia' rappresentato dalle metriche superiori della CAMPO
            # (KM Rimanenti / Tempo rimanente), quindi non lo ripetiamo qui.
            # Lasciamo soltanto il comando TERMINA GIRO quando tutte le consegne
            # sono state gestite.
            if st.session_state.vista_giro == "CAMPO":
                if tutte_gestite and not giro_terminato:
                    st.info("Tutte le consegne sono gestite. Rientra in sede e premi TERMINA GIRO.")
                    if st.button("🏁 TERMINA GIRO", use_container_width=True, type="primary", key="btn_termina_giro"):
                        if st.session_state.get("inizio_giro_reale") is None:
                            st.warning("⚠️ Devi prima premere INIZIA GIRO.")
                        else:
                            st.session_state.fine_giro_reale = time.time()
                            st.session_state.giro_terminato = True
                            salva_stato_giro_persistente(st.session_state.utente_corrente)
                            st.rerun()
            st.markdown("---")
        if not st.session_state.giro_corrente.empty:
            st.session_state.giro_corrente['POSIZIONE'] = [str(i) for i in range(1, len(st.session_state.giro_corrente) + 1)]
            # PREPARAZIONE mantiene sempre l'ordine reale del giro salvato.
            # RIEPILOGO/CAMPO usano invece la vista operativa con i gestiti in fondo.
            df_giro_preparazione = st.session_state.giro_corrente.copy().reset_index(drop=True)
            df_vista_giro = prepara_vista_giro(st.session_state.giro_corrente)
            
            # Il percorso NON viene riottimizzato automaticamente.
            # Si mantiene l'ordine gia' ottimizzato e si escludono solo le consegne gia' gestite.
            addresses = indirizzi_per_percorso_giro(st.session_state.giro_corrente)
            partenza = indirizzo_partenza_giro(st.session_state.giro_corrente)
            if addresses:
                origin = urllib.parse.quote(partenza)
                if len(addresses) == 1:
                    maps_url = f"https://www.google.com/maps/dir/{origin}/{urllib.parse.quote(addresses[0])}"
                else:
                    destination = urllib.parse.quote(addresses[-1])
                    if len(addresses) > 1:
                        waypoints = "/".join([urllib.parse.quote(a) for a in addresses[:-1]])
                        maps_url = f"https://www.google.com/maps/dir/{origin}/{waypoints}/{destination}"
                    else:
                        maps_url = f"https://www.google.com/maps/dir/{origin}/{destination}"
            else:
                maps_url = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(DEPOSITO_VANGO)}"

            if st.session_state.vista_giro == "RIEPILOGO":
                st.markdown(f"<p style='color: #94A3B8; font-size: 14px; margin-bottom: 15px;'>{tot_clienti} indirizzi trovati nel giro.</p>", unsafe_allow_html=True)

                st.markdown('<div id="avvia-percorso-top"></div>', unsafe_allow_html=True)
                st.markdown(f'''
                    <a href="{maps_url}" target="_blank" style="text-decoration:none;">
                        <button style="width:100%; background-color:#2563EB; color:white; border:none; border-radius:25px; height:52px; font-weight:bold; font-size:16px; box-shadow:0 4px 10px rgba(37,99,235,0.4);">
                            🗺️ AVVIA PERCORSO
                        </button>
                    </a>
                ''', unsafe_allow_html=True)
                st.markdown('<div style="height:10px;"></div>', unsafe_allow_html=True)

                for idx in range(tot_clienti):
                    row = df_vista_giro.iloc[idx]
                    idx_reale = int(row["__IDX_ORIGINALE"])

                    # Card riepilogo: il cestino e' integrato NELLA STESSA CARD, a destra.
                    # Usiamo un container con bordo per evitare che il pulsante finisca sotto la card.
                    card_opacita = 0.52 if str(row.get("STATO", "")).strip() in [STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO] else 1.0
                    with st.container(border=True):
                        col_badge, col_info = st.columns([0.10, 0.90], gap="small", vertical_alignment="top")

                        with col_badge:
                            st.markdown(f'<div class="clean-badge" style="opacity:{card_opacita};">{idx + 1}</div>', unsafe_allow_html=True)

                        with col_info:
                            st.markdown(f"""
                            <div class="clean-content" style="opacity:{card_opacita};">
                                <div class="clean-title">{row['CLIENTE']}</div>
                                <div class="clean-subtitle">📍 {row['VIA']}, {row['COMUNE']} (🕒 {row['ORA']} | 📦 {row['Q.ta']} pz)</div>
                            </div>
                            """, unsafe_allow_html=True)
                            stato_attuale = str(row.get('STATO', '')).strip() or STATO_DA_FARE
                            if stato_attuale not in STATI_CONSEGNA:
                                stato_attuale = STATO_DA_FARE
                            stato_nuovo = st.selectbox(
                                "Stato consegna",
                                options=STATI_CONSEGNA,
                                index=STATI_CONSEGNA.index(stato_attuale),
                                key=f"stato_consegna_pulita_{idx_reale}_{row['CLIENTE']}"
                            )
                            if stato_nuovo != stato_attuale:
                                salva_stato_consegna(idx_reale, stato_nuovo)

                st.markdown("---")
                st.markdown('''
                <div style="text-align:center; margin:4px 0 8px 0;">
                    <a href="#avvia-percorso-top" style="text-decoration:none; font-size:28px;">⬆️</a>
                </div>
                ''' , unsafe_allow_html=True)
            elif st.session_state.vista_giro == "CAMPO":
                # V10.3.3: dashboard CAMPO pulita e ottimizzata per l'uso durante la guida.
                # Palette e icone restano coerenti con l'interfaccia VanGo.
                st.markdown("""
                <style>
                    /* CAMPO mobile: tutto resta responsive. Solo la riga cliente
                       mantiene cliente a sinistra + comando stato a destra. */
                    @media (max-width: 640px) {
                        [class*="st-key-campo_riga_"] {
                            width: 100% !important;
                            max-width: 100% !important;
                            min-width: 0 !important;
                            box-sizing: border-box !important;
                            overflow: hidden !important;
                        }
                        [class*="st-key-campo_riga_"] [data-testid="stHorizontalBlock"] {
                            width: 100% !important;
                            max-width: 100% !important;
                            min-width: 0 !important;
                            flex-wrap: nowrap !important;
                            box-sizing: border-box !important;
                            align-items: center !important;
                        }
                        [class*="st-key-campo_riga_"] [data-testid="column"] {
                            min-width: 0 !important;
                            box-sizing: border-box !important;
                        }
                        [class*="st-key-campo_riga_"] [data-testid="column"]:first-child {
                            flex: 1 1 auto !important;
                            width: auto !important;
                            min-width: 0 !important;
                            overflow: hidden !important;
                        }
                        [class*="st-key-campo_riga_"] [data-testid="column"]:last-child {
                            flex: 0 0 46px !important;
                            width: 46px !important;
                            max-width: 46px !important;
                            min-width: 46px !important;
                            padding: 0 !important;
                            margin: 0 !important;
                        }
                    }

                    /* La card cliente non deve mai superare la larghezza disponibile. */
                    [class*="st-key-campo_riga_"] {
                        width: 100% !important;
                        max-width: 100% !important;
                        min-width: 0 !important;
                        box-sizing: border-box !important;
                        overflow: hidden !important;
                        padding-top: 0 !important;
                        padding-bottom: 0 !important;
                    }

                    /* Il trigger del popover e' SOLO la freccia: nessun riquadro bianco.
                       La larghezza della freccia non modifica la larghezza della card. */
                    [class*="st-key-campo_stato_menu_"] {
                        width: 46px !important;
                        max-width: 46px !important;
                        min-width: 46px !important;
                        padding: 0 !important;
                        margin: 0 0 0 auto !important;
                        box-sizing: border-box !important;
                    }
                    /* Fallback robusto: nelle versioni di Streamlit in cui la key del
                       popover non avvolge il pulsante trigger, prendiamo direttamente
                       il contenitore stPopover. */
                    [data-testid="stPopover"] {
                        width: 46px !important;
                        max-width: 46px !important;
                        min-width: 46px !important;
                        padding: 0 !important;
                        margin: 0 0 0 auto !important;
                        box-sizing: border-box !important;
                    }
                    [data-testid="stPopover"] > button,
                    [data-testid="stPopover"] button {
                        width: 46px !important;
                        max-width: 46px !important;
                        min-width: 46px !important;
                        height: 42px !important;
                        min-height: 42px !important;
                        padding: 0 !important;
                        margin: 0 !important;
                        border: 0 !important;
                        border-radius: 8px !important;
                        background: transparent !important;
                        box-shadow: none !important;
                        color: {colore_freccia} !important;
                        font-size: 27px !important;
                        font-weight: 900 !important;
                        line-height: 42px !important;
                        text-align: center !important;
                    }
                    /* V10.3.19: selettore robusto del trigger popover. */
                    button[aria-haspopup="dialog"] {{
                        width: 100% !important;
                        max-width: 100% !important;
                        min-width: 0 !important;
                        box-sizing: border-box !important;
                        justify-content: flex-start !important;
                        text-align: left !important;
                        margin-left: 0 !important;
                        margin-right: 0 !important;
                    }}
                    button[aria-haspopup="dialog"] > div,
                    button[aria-haspopup="dialog"] > div > div,
                    button[aria-haspopup="dialog"] [data-testid="stMarkdownContainer"],
                    button[aria-haspopup="dialog"] [data-testid="stMarkdownContainer"] > div,
                    button[aria-haspopup="dialog"] p,
                    button[aria-haspopup="dialog"] span {{
                        width: 100% !important;
                        max-width: 100% !important;
                        min-width: 0 !important;
                        box-sizing: border-box !important;
                        margin-left: 0 !important;
                        margin-right: 0 !important;
                        padding-left: 0 !important;
                        padding-right: 0 !important;
                        text-align: left !important;
                        justify-content: flex-start !important;
                        align-items: flex-start !important;
                        align-self: flex-start !important;
                    }}
                    button[aria-haspopup="dialog"] svg {{
                        display: none !important;
                        width: 0 !important;
                        max-width: 0 !important;
                        flex: 0 0 0 !important;
                    }}
                    [data-testid="stPopover"] > button:hover,
                    [data-testid="stPopover"] > button:focus,
                    [data-testid="stPopover"] > button:active {
                        background: transparent !important;
                        box-shadow: none !important;
                    }

                    [class*="st-key-campo_stato_menu_"] button {
                        width: 46px !important;
                        max-width: 46px !important;
                        min-width: 46px !important;
                        height: 42px !important;
                        min-height: 42px !important;
                        padding: 0 !important;
                        margin: 0 !important;
                        border: 0 !important;
                        border-radius: 8px !important;
                        background: transparent !important;
                        box-shadow: none !important;
                        color: {colore_freccia} !important;
                        font-size: 27px !important;
                        font-weight: 900 !important;
                        line-height: 42px !important;
                        text-align: center !important;
                    }
                    [class*="st-key-campo_stato_menu_"] button:hover,
                    [class*="st-key-campo_stato_menu_"] button:focus,
                    [class*="st-key-campo_stato_menu_"] button:active {
                        background: transparent !important;
                        box-shadow: none !important;
                    }
                    @media (max-width: 640px) {
                        [class*="st-key-campo_stato_menu_"] {
                            width: 46px !important;
                            max-width: 46px !important;
                            min-width: 46px !important;
                        }
                        [class*="st-key-campo_stato_menu_"] button {
                            width: 46px !important;
                            max-width: 46px !important;
                            min-width: 46px !important;
                            font-size: 25px !important;
                        }
                    }
                    .campo-metric-card {
                        background: linear-gradient(135deg, #142033 0%, #0F1724 100%);
                        border: 1px solid #26384F;
                        border-radius: 14px;
                        padding: 14px 15px;
                        min-height: 82px;
                        box-sizing: border-box;
                    }
                    .campo-metric-label { color:#A9C4EA; font-size:11px; font-weight:700; margin-bottom:5px; }
                    .campo-metric-value { color:#FFFFFF; font-size:22px; font-weight:800; line-height:1.05; }
                    .campo-metric-sub { color:#7894BC; font-size:11px; margin-top:4px; }
                    .campo-next-card {
                        background: linear-gradient(135deg, #142033 0%, #101A29 100%);
                        border:1px solid #26384F; border-radius:14px; padding:14px 16px; margin:4px 0 8px 0;
                    }
                    .campo-next-title { color:#FFFFFF; font-size:16px; font-weight:800; }
                    .campo-next-sub { color:#A9C4EA; font-size:13px; margin-top:3px; }
                    .campo-next-badge { color:#FFFFFF; background:#1E293B; border:1px solid #334A68; border-radius:10px; padding:7px 10px; font-size:11px; font-weight:700; }
                    .campo-return-space { min-height: 1px; }
                    /* Colori dei soli comandi operativi presenti nella vista CAMPO. */
                    button[data-testid="stBaseButton-primary"] { background:#10B981 !important; border-color:#10B981 !important; color:#FFFFFF !important; border-radius:14px !important; font-weight:800 !important; }
                    button[data-testid="stBaseButton-secondary"] { background:#111C2B !important; border-color:#334A68 !important; color:#FFFFFF !important; border-radius:14px !important; font-weight:800 !important; }
                </style>
                """, unsafe_allow_html=True)

                # Azioni principali: verde = inizio, blu = navigazione, scuro = fine.
                a1, a2, a3 = st.columns(3, gap="small")
                with a1:
                    if not st.session_state.get("giro_terminato", False) and st.session_state.get("inizio_giro_reale") is None:
                        if st.button("▶️  INIZIA GIRO", use_container_width=True, type="primary", key="btn_inizia_giro"):
                            st.session_state.inizio_giro_reale = time.time()
                            st.session_state.fine_giro_reale = None
                            st.session_state.giro_terminato = False
                            salva_stato_giro_persistente(st.session_state.utente_corrente)
                            st.rerun()
                    else:
                        st.button("▶️  GIRO INIZIATO", use_container_width=True, disabled=True, key="btn_giro_iniziato")
                with a2:
                    st.markdown(f"""
                        <a href="{maps_url}" target="_blank" style="text-decoration:none;">
                            <button style="width:100%; background:#2563EB; color:white; border:1px solid #3B82F6; border-radius:14px; height:46px; font-weight:800; font-size:14px; box-shadow:0 4px 12px rgba(37,99,235,.28);">🗺️  AVVIA PERCORSO</button>
                        </a>
                    """, unsafe_allow_html=True)
                with a3:
                    if tutte_gestite and not st.session_state.get("giro_terminato", False) and st.session_state.get("inizio_giro_reale") is not None:
                        if st.button("🏁  TERMINA GIRO", use_container_width=True, type="secondary", key="btn_termina_giro_dashboard"):
                            st.session_state.fine_giro_reale = time.time()
                            st.session_state.giro_terminato = True
                            salva_stato_giro_persistente(st.session_state.utente_corrente)
                            st.rerun()
                    else:
                        st.button("🏁  TERMINA GIRO", use_container_width=True, disabled=True, key="btn_termina_giro_dashboard_disabled")

                # FERMO MEZZO / PAUSA: il tempo viene sottratto dal tempo effettivo usato
                # per il confronto anticipo/ritardo e resta memorizzato anche dopo un rerun.
                if st.session_state.get("fermo_mezzo_attivo", False):
                    if st.button("⏸️  PAUSA TERMINATA", use_container_width=True, type="primary", key="btn_pausa_terminata"):
                        termina_fermo_mezzo()
                        st.rerun()
                    st.warning(f"⏸️ FERMO MEZZO attivo — tempo escluso dal ritardo: {_formatta_durata_hm(_minuti_fermo_totali())}")
                elif not st.session_state.get("giro_terminato", False):
                    if st.button("⏸️  FERMO MEZZO", use_container_width=True, key="btn_fermo_mezzo"):
                        avvia_fermo_mezzo()
                        st.rerun()
                    if _minuti_fermo_totali() > 0:
                        st.caption(f"⏸️ Tempo totale di fermo registrato: {_formatta_durata_hm(_minuti_fermo_totali())}")

                if not st.session_state.get("giro_terminato", False) and st.session_state.get("inizio_giro_reale") is None:
                    st.caption("🕐 Premi INIZIA GIRO per avviare il tempo effettivo del giro.")
                elif st.session_state.get("inizio_giro_reale") is not None and not st.session_state.get("giro_terminato", False):
                    st.caption(f"🕐 Giro iniziato alle {_formatta_ora_partenza_reale()}: il tempo effettivo viene calcolato fino a TERMINA GIRO.")

                # ------------------------------------------------------------
                # GPS LIVE - V10.5.8 V3 TEST_3
                # Una sola istanza GPS viene creata qui. Il fragment si aggiorna
                # automaticamente ogni 60 secondi e la mappa viene renderizzata
                # nello stesso fragment, cosi' si aggiorna insieme alle coordinate.
                if not st.session_state.get('giro_terminato', False):
                    _gps_live_refresh()

                # POSIZIONE ATTUALE: se il GPS e' attivo usiamo la posizione
                # reale del telefono; in assenza di GPS manteniamo il comportamento
                # precedente basato sull'ultima consegna gestita/deposito.
                # Il dataframe usato dalle metriche deve essere disponibile
                # indipendentemente dal fatto che il GPS sia attivo o meno.
                # In precedenza veniva creato solo nel ramo senza GPS, causando
                # un NameError quando il GPS forniva correttamente la posizione.
                df_pos = st.session_state.giro_corrente.copy()
                if "STATO" not in df_pos.columns:
                    df_pos["STATO"] = STATO_DA_FARE

                gps_via = str(st.session_state.get('gps_via', '') or '').strip()
                gps_comune = str(st.session_state.get('gps_comune', '') or '').strip()
                if st.session_state.get('gps_attivo', False) and (gps_via or gps_comune):
                    # Mostra sempre l'indirizzo su due righe:
                    # Via + civico / Comune (es. Via Daniele Manin / Vimercate).
                    posizione_label = gps_via or "Posizione GPS"
                    posizione_comune = gps_comune or "Posizione rilevata dal telefono"
                else:
                    stati_pos = df_pos["STATO"].fillna("").astype(str).str.upper()
                    mask_gestiti_pos = stati_pos.str.contains("FATTO|PARZIALE|RESPINTO", regex=True)
                    gestiti_pos = df_pos[mask_gestiti_pos]
                    if not gestiti_pos.empty:
                        ultima = gestiti_pos.iloc[-1]
                        posizione_label = str(ultima.get("VIA", "")).strip() or "Ultima consegna"
                        posizione_comune = str(ultima.get("COMUNE", "")).strip()
                    else:
                        posizione_label = DEPOSITO_VANGO
                        posizione_comune = "Punto di partenza"

                df_metriche_campo = df_pos[~df_pos["STATO"].fillna("").astype(str).isin([STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO])].copy()
                residui_campo = len(df_metriche_campo)
                gestiti_campo = max(0, len(df_pos) - residui_campo)
                km_campo_display = km_visualizzati if km_visualizzati is not None else 0.0
                minuti_campo_display = minuti_visualizzati if minuti_visualizzati is not None else 0.0
                tempo_campo_display = _formatta_durata_hm(minuti_campo_display)

                m1, m2, m3, m4 = st.columns(4, gap="small")
                with m1:
                    st.markdown(f'<div class="campo-metric-card"><div class="campo-metric-label">📦 CONSEGNE RIMANENTI</div><div class="campo-metric-value">{residui_campo} / {len(df_pos)}</div><div class="campo-metric-sub">{gestiti_campo} gestite</div></div>', unsafe_allow_html=True)
                with m2:
                    st.markdown(f'<div class="campo-metric-card"><div class="campo-metric-label">🛣️ KM RESIDUI</div><div class="campo-metric-value">{float(km_campo_display):.1f} km</div><div class="campo-metric-sub">incluso rientro in sede</div></div>', unsafe_allow_html=True)
                with m3:
                    st.markdown(f'<div class="campo-metric-card"><div class="campo-metric-label">🕐 TEMPO RESIDUO</div><div class="campo-metric-value">{tempo_campo_display}</div><div class="campo-metric-sub">incluso rientro in sede</div></div>', unsafe_allow_html=True)
                with m4:
                    st.markdown(f'<div class="campo-metric-card"><div class="campo-metric-label">📍 POSIZIONE ATTUALE</div><div class="campo-metric-value" style="font-size:15px;">{posizione_label}</div><div class="campo-metric-sub">{posizione_comune}</div></div>', unsafe_allow_html=True)

                totali_colli_campo = _totali_colli_giro(st.session_state.giro_corrente)
                c1c, c2c, c3c = st.columns(3, gap="small")
                with c1c:
                    st.markdown(f'<div class="campo-metric-card"><div class="campo-metric-label">📦 COLLI DA CONSEGNARE</div><div class="campo-metric-value">{totali_colli_campo["residui"]}</div><div class="campo-metric-sub">su {totali_colli_campo["iniziali"]} iniziali</div></div>', unsafe_allow_html=True)
                with c2c:
                    st.markdown(f'<div class="campo-metric-card"><div class="campo-metric-label">✅ COLLI CONSEGNATI</div><div class="campo-metric-value">{totali_colli_campo["consegnati"]}</div><div class="campo-metric-sub">effettivamente consegnati</div></div>', unsafe_allow_html=True)
                with c3c:
                    st.markdown(f'<div class="campo-metric-card"><div class="campo-metric-label">↩️ COLLI DA RENDERE</div><div class="campo-metric-value">{totali_colli_campo["da_rendere"]}</div><div class="campo-metric-sub">rifiutati</div></div>', unsafe_allow_html=True)

                st.markdown("<div style='height:14px'></div>", unsafe_allow_html=True)

                # V10.4.1: confronto live con la previsione cumulativa
                # specifica dell'ultima fermata realmente gestita.
                previsione_avanzamento = st.session_state.get("previsione_giro") or {}
                stato_avanzamento = _stato_avanzamento_giro(
                    gestiti_campo, len(df_pos), previsione_avanzamento.get("minuti")
                )
                if stato_avanzamento is not None:
                    st.markdown(f"""
                    <div style="border:1px solid {stato_avanzamento['colore']}55; border-radius:12px; padding:10px 14px; margin-bottom:14px; background:{stato_avanzamento['colore']}14; display:flex; justify-content:space-between; align-items:center;">
                        <span style="font-size:15px; font-weight:700;">{stato_avanzamento['emoji']} {stato_avanzamento['testo']}</span>
                        <span style="font-size:12px; color:#94A3B8;">{stato_avanzamento['dettaglio']}</span>
                    </div>
                    """, unsafe_allow_html=True)
                elif gestiti_campo == 0:
                    st.caption("ℹ️ Il confronto con la previsione apparirà dopo la prima consegna gestita.")

                st.markdown("<div style='font-size:16px; font-weight:800; color:#FFFFFF; margin:0 0 8px 4px;'>📍 PROSSIMA CONSEGNA</div>", unsafe_allow_html=True)

                # CAMPO: mostra SOLO le consegne ancora da gestire.
                # Il filtro viene applicato direttamente al giro reale, prima della
                # preparazione grafica, cosi' i clienti gestiti non possono rientrare
                # nella lista per effetto del riordinamento della vista.
                # Usiamo anche una normalizzazione robusta del testo dello stato,
                # cosi' eventuali spazi/variazioni non fanno ricomparire una consegna.
                def _stato_gestito_campo(valore):
                    testo = str(valore if valore is not None else "").strip().upper()
                    return (
                        testo == str(STATO_FATTO).strip().upper()
                        or testo == str(STATO_PARZIALE).strip().upper()
                        or testo == str(STATO_RESPINTO).strip().upper()
                        or "FATTO" in testo
                        or "PARZIALE" in testo
                        or "RESPINTO" in testo
                    )

                # CAMPO = SOLO CLIENTI DA FARE.
                # Nascondiamo inoltre in modo esplicito e persistente nella sessione
                # il cliente appena lavorato: cosi' deve sparire dalla CAMPO anche se
                # Google Sheets impiega qualche istante ad aggiornarsi.
                if "campo_clienti_nascosti" not in st.session_state:
                    st.session_state.campo_clienti_nascosti = set()

                df_campo_base = st.session_state.giro_corrente.copy().reset_index(drop=True)
                if "STATO" not in df_campo_base.columns:
                    df_campo_base["STATO"] = STATO_DA_FARE
                df_campo_base["STATO"] = df_campo_base["STATO"].fillna("").astype(str)
                df_campo_base["__IDX_ORIGINALE"] = list(range(len(df_campo_base)))

                indici_pendenti_campo = []
                for i, row_campo in df_campo_base.iterrows():
                    cliente_key = f"{i}|{str(row_campo.get('CLIENTE', '')).strip()}|{str(row_campo.get('COMUNE', '')).strip()}|{str(row_campo.get('VIA', '')).strip()}"
                    valore_stato = row_campo.get("STATO", "")
                    if not _stato_gestito_campo(valore_stato) and cliente_key not in st.session_state.campo_clienti_nascosti:
                        indici_pendenti_campo.append(i)
                df_campo_pendenti = df_campo_base.iloc[indici_pendenti_campo].copy().reset_index(drop=True)

                for idx, (_, row) in enumerate(df_campo_pendenti.iterrows()):
                    idx_reale = int(row["__IDX_ORIGINALE"])
                    stato_attuale = str(row.get("STATO", "")).strip() or STATO_DA_FARE
                    if stato_attuale not in STATI_CONSEGNA:
                        stato_attuale = STATO_DA_FARE

                    # V10.3.20: CAMPO smartphone - niente popover.
                    # Il nome cliente e' un normale pulsante Streamlit, quindi il testo
                    # viene allineato realmente a sinistra. Al click si apre subito sotto
                    # un piccolo menu con i quattro stati. Niente freccia e niente CSS
                    # dipendente dalla struttura interna di st.popover.
                    if "campo_menu_aperto" not in st.session_state:
                        st.session_state.campo_menu_aperto = None

                    with st.container(key=f"campo_riga_{idx_reale}"):
                        stato_nuovo = stato_attuale

                        st.markdown(f"""
                        <style>
                        [class*="st-key-campo_riga_{idx_reale}"] {{
                            width: 100% !important;
                            max-width: 100% !important;
                            min-width: 0 !important;
                            box-sizing: border-box !important;
                            overflow: hidden !important;
                            padding: 0 !important;
                            margin: 0 !important;
                        }}
                        [class*="st-key-campo_riga_{idx_reale}"] [data-testid="stVerticalBlock"] {{
                            gap: 0 !important;
                            padding: 0 !important;
                            margin: 0 !important;
                        }}
                        [class*="st-key-campo_cliente_btn_{idx_reale}"] {{
                            width: 100% !important;
                            max-width: 100% !important;
                            margin: 0 !important;
                            padding: 0 !important;
                        }}
                        [class*="st-key-campo_cliente_btn_{idx_reale}"] button {{
                            width: 100% !important;
                            max-width: 100% !important;
                            min-width: 0 !important;
                            min-height: 30px !important;
                            height: 30px !important;
                            padding: 0 6px !important;
                            margin: 0 !important;
                            border: 1px solid #39475A !important;
                            border-radius: 8px !important;
                            background: #1E293B !important;
                            box-shadow: none !important;
                            color: #FFFFFF !important;
                            font-size: 16px !important;
                            font-weight: 800 !important;
                            line-height: 30px !important;
                            text-align: left !important;
                            justify-content: flex-start !important;
                            align-items: center !important;
                            white-space: nowrap !important;
                            overflow: hidden !important;
                            text-overflow: ellipsis !important;
                        }}
                        [class*="st-key-campo_cliente_btn_{idx_reale}"] button > div,
                        [class*="st-key-campo_cliente_btn_{idx_reale}"] button > div > div,
                        [class*="st-key-campo_cliente_btn_{idx_reale}"] button p,
                        [class*="st-key-campo_cliente_btn_{idx_reale}"] button span {{
                            width: auto !important;
                            max-width: 100% !important;
                            margin: 0 !important;
                            padding: 0 !important;
                            text-align: left !important;
                            justify-content: flex-start !important;
                        }}
                        [class*="st-key-campo_menu_stati_{idx_reale}"] {{
                            width: 100% !important;
                            max-width: 100% !important;
                            margin: 0 !important;
                            padding: 0 !important;
                        }}
                        [class*="st-key-campo_menu_stati_{idx_reale}"] button {{
                            min-height: 34px !important;
                            margin: 2px 0 !important;
                            font-size: 14px !important;
                        }}
                        </style>
                        """, unsafe_allow_html=True)

                        if st.button(
                            str(row['CLIENTE']),
                            key=f"campo_cliente_btn_{idx_reale}",
                            use_container_width=True,
                        ):
                            if st.session_state.campo_menu_aperto == idx_reale:
                                st.session_state.campo_menu_aperto = None
                            else:
                                st.session_state.campo_menu_aperto = idx_reale
                            st.rerun()

                        if st.session_state.campo_menu_aperto == idx_reale:
                            with st.container(key=f"campo_menu_stati_{idx_reale}"):
                                st.caption("Stato consegna")
                                if st.session_state.get("campo_parziale_idx") == idx_reale:
                                    qta_riga = max(0, int(round(float(row.get("Q.ta", 0) or 0))))
                                    consegnati_parziali = st.number_input(
                                        f"Colli consegnati (su {qta_riga})",
                                        min_value=0, max_value=qta_riga,
                                        value=min(qta_riga, int(st.session_state.get(f"campo_parziale_qta_{idx_reale}", 0) or 0)),
                                        key=f"campo_parziale_qta_input_{idx_reale}"
                                    )
                                    if st.button("✅ CONFERMA PARZIALE", use_container_width=True, key=f"campo_conferma_parziale_{idx_reale}"):
                                        salva_stato_consegna(idx_reale, STATO_PARZIALE, consegnati_parziali)
                                        st.session_state.campo_parziale_idx = None
                                        st.session_state.campo_menu_aperto = None
                                        st.rerun()
                                    if st.button("↩️ ANNULLA", use_container_width=True, key=f"campo_annulla_parziale_{idx_reale}"):
                                        st.session_state.campo_parziale_idx = None
                                        st.session_state.campo_menu_aperto = None
                                        st.rerun()
                                # Menu stati verticale: una voce sotto l'altra.
                                for opzione_stato in STATI_CONSEGNA:
                                    if st.button(
                                        opzione_stato,
                                        key=f"campo_stato_opzione_{idx_reale}_{opzione_stato}",
                                        use_container_width=True,
                                    ):
                                        if opzione_stato == STATO_PARZIALE:
                                            st.session_state.campo_parziale_idx = idx_reale
                                            st.session_state.campo_menu_aperto = idx_reale
                                            st.rerun()
                                        else:
                                            salva_stato_consegna(idx_reale, opzione_stato)
                                            st.session_state.campo_menu_aperto = None
                                            st.rerun()

                    if stato_nuovo != stato_attuale:
                        # Compatibilita' con eventuali selezioni residue: usa sempre il
                        # percorso unico che registra anche i colli consegnati/rifiutati.
                        salva_stato_consegna(idx_reale, stato_nuovo)
                        st.session_state.campo_menu_aperto = None
                        st.rerun()
                        # Codice legacy non raggiungibile, mantenuto fuori dal flusso operativo.
                        df_reale = st.session_state.giro_corrente.copy().reset_index(drop=True)
                        if 0 <= idx_reale < len(df_reale):
                            if "STATO" not in df_reale.columns:
                                df_reale["STATO"] = STATO_DA_FARE
                            df_reale.at[idx_reale, "STATO"] = stato_nuovo
                            st.session_state.giro_corrente = df_reale
                            st.session_state.fine_giro_reale = None
                            st.session_state.giro_terminato = False
                            # Un nuovo cambio stato riapre il giro: aggiorna anche lo stato persistente.
                            salva_stato_giro_persistente(st.session_state.utente_corrente)
                            # Nascondi IMMEDIATAMENTE il cliente dalla CAMPO.
                            # La chiave include indice + cliente + comune + via per
                            # evitare che il widget possa farlo ricomparire al rerun.
                            r = df_reale.iloc[idx_reale]
                            cliente_key = f"{idx_reale}|{str(r.get('CLIENTE', '')).strip()}|{str(r.get('COMUNE', '')).strip()}|{str(r.get('VIA', '')).strip()}"
                            st.session_state.campo_clienti_nascosti.add(cliente_key)
                            salva_giro_utente_su_sheets(st.session_state.utente_corrente, df_reale)
                        st.rerun()
                st.markdown("---")
                st.markdown('''
                <div style="text-align:center; margin:4px 0 8px 0;">
                    <a href="#avvia-percorso-top" style="text-decoration:none; font-size:28px;">⬆️</a>
                </div>
                ''' , unsafe_allow_html=True)
            elif st.session_state.vista_giro == "PREPARAZIONE":
                # Vista di preparazione: ordine originale del giro, senza spostare
                # visivamente in fondo i clienti gia' gestiti. Qui non si gestiscono
                # gli stati di consegna.
                st.markdown('<div id="avvia-percorso-top"></div>', unsafe_allow_html=True)
                st.markdown(f"""
                    <a href="{maps_url}" target="_blank" style="text-decoration:none;">
                        <button style="width:100%; background-color:#2563EB; color:white; border:none; border-radius:25px; height:52px; font-weight:bold; font-size:16px; box-shadow:0 4px 10px rgba(37,99,235,0.4);">
                            🗺️ AVVIA PERCORSO
                        </button>
                    </a>
                """, unsafe_allow_html=True)
                st.markdown('<div style="height:10px;"></div>', unsafe_allow_html=True)

                for idx in range(len(df_giro_preparazione)):
                    row = df_giro_preparazione.iloc[idx]
                    st.markdown(f"""
                    <div class="stop-card" style="opacity:1.0;">
                        <div class="stop-title">{idx + 1}. {row['CLIENTE']}</div>
                        <div class="stop-address">📍 {row['VIA']}, {row['COMUNE']}</div>
                        <div class="stop-meta">🕒 Ora: {row['ORA']} | 📦 Q.tà: {row['Q.ta']} pz</div>
                    </div>
                    """, unsafe_allow_html=True)

                    col_c1, col_c2, col_c3, col_c4 = st.columns([1, 1, 1, 1])

                    with col_c1:
                        dest = urllib.parse.quote(f"{row['VIA']}, {row['COMUNE']}")
                        st.write("")
                        st.markdown(f"[🚘 **NAVIGA ORA**](https://www.google.com/maps/dir/?api=1&destination={dest})")

                    with col_c2:
                        nuova_qta = st.number_input(
                            "Q.tà colli",
                            min_value=0,
                            value=int(row['Q.ta']),
                            key=f"qta_preparazione_{row['CLIENTE']}_{idx}"
                        )
                        if nuova_qta != int(row['Q.ta']):
                            st.session_state.giro_corrente.at[idx, 'Q.ta'] = nuova_qta
                            salva_giro_utente_su_sheets(st.session_state.utente_corrente, st.session_state.giro_corrente)
                            st.rerun()

                    with col_c3:
                        # Spostamento manuale dei soli clienti ancora da consegnare.
                        stato_riga = str(row.get("STATO", "")).strip()
                        stati_gestiti = [STATO_FATTO, STATO_PARZIALE, STATO_RESPINTO]
                        if stato_riga not in stati_gestiti:
                            clienti_pendenti = df_giro_preparazione.iloc[:sum(
                                1 for _, rr in df_giro_preparazione.iterrows()
                                if str(rr.get("STATO", "")).strip() not in stati_gestiti
                            )]
                            numero_pendenti = len(clienti_pendenti)
                            posizione_pendente = next(
                                (i + 1 for i, rr in clienti_pendenti.iterrows() if int(rr.name) == idx),
                                1
                            )
                            nuova_pos = st.selectbox(
                                "Sposta a pos:",
                                options=list(range(1, numero_pendenti + 1)),
                                index=posizione_pendente - 1,
                                key=f"select_pos_preparazione_{row['CLIENTE']}_{idx}"
                            )
                            if nuova_pos != posizione_pendente:
                                if sposta_cliente_pendente_nella_posizione(idx, nuova_pos):
                                    st.rerun()
                        else:
                            st.caption("🔒 Gestito")

                    with col_c4:
                        if st.button("🗑️", help="Elimina cliente dal giro", key=f"elimina_preparazione_{idx}_{row['CLIENTE']}"):
                            st.session_state.conferma_eliminazione_idx = idx
                            st.rerun()

                        if st.session_state.conferma_eliminazione_idx == idx:
                            st.warning(f"Eliminare {row['CLIENTE']} dal giro?")
                            c_ok, c_no = st.columns(2)
                            with c_ok:
                                if st.button("✅ CONFERMA", use_container_width=True, key=f"conferma_elimina_preparazione_{idx}"):
                                    elimina_cliente_dal_giro(idx)
                            with c_no:
                                if st.button("❌ ANNULLA", use_container_width=True, key=f"annulla_elimina_preparazione_{idx}"):
                                    st.session_state.conferma_eliminazione_idx = None
                                    st.rerun()

                    st.markdown("<hr style=\"margin: 10px 0; border-color: #262626;\">", unsafe_allow_html=True)
                st.markdown("---")
                st.markdown('''
                <div style="text-align:center; margin:4px 0 8px 0;">
                    <a href="#avvia-percorso-top" style="text-decoration:none; font-size:28px;">⬆️</a>
                </div>
                ''' , unsafe_allow_html=True)
        else:
            st.info("Nessuna fermata nel tuo giro corrente. Clicca in alto su '📁 CLIENTI' per aggiungerne.")

    # ==========================================
    # SCHERMATA ANALISI
    # ==========================================
    elif st.session_state.pagina_attiva == "analisi":
        render_analisi()

    # ==========================================
    # SCHERMATA 2: INSERISCI CLIENTE
    # ==========================================
    elif st.session_state.pagina_attiva == "db":
        st.subheader("📁 Inserisci Clienti nel Tuo Giro")
        
        # Pulsante universale per forzare l'aggiornamento e svuotare la cache
        if st.button("🔄 Forza Aggiornamento / Svuota Cache", use_container_width=True):
            st.cache_data.clear()
            st.session_state.db_clienti = carica_db_da_google_sheets()
            st.success("Cache svuotata e dati ricaricati con successo!")
            st.rerun()
            
        st.markdown("<br>", unsafe_allow_html=True)

        if st.session_state.is_admin and not st.session_state.db_clienti.empty:
            if st.button("🌍 GELOCALIZZA CLIENTI E SALVA COORDINATE", use_container_width=True, key="btn_geolocalizza_clienti"):
                try:
                    with st.spinner("🌍 Geolocalizzo i clienti senza coordinate... e salvo progressivamente la colonna H"):
                        df_geo, trovati_geo, gia_presenti_geo, non_trovati_geo = geolocalizza_tutti_clienti(
                            st.session_state.db_clienti,
                            salvataggio_progressivo=salva_coordinate_su_google_sheets
                        )
                        st.session_state.db_clienti = df_geo
                        salva_coordinate_su_google_sheets(st.session_state.db_clienti)
                    st.success(f"✅ Coordinate aggiornate: {trovati_geo} nuovi clienti. {gia_presenti_geo} erano già geolocalizzati.")
                    if non_trovati_geo:
                        elenco_geo = "\n".join(f"- {x}" for x in non_trovati_geo[:8])
                        if len(non_trovati_geo) > 8:
                            elenco_geo += f"\n- ... e altri {len(non_trovati_geo) - 8}"
                        st.warning("⚠️ Non sono riuscito a trovare questi clienti:\n" + elenco_geo)
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ Geolocalizzazione non riuscita: {e}")

        if st.session_state.is_admin:
            st.info("🔒 PROTEZIONE DATABASE: Foglio1 è in sola lettura. L'app può scrivere esclusivamente le COORDINATE in colonna H.")
            st.caption("Il caricamento/sovrascrittura dell'anagrafica da questa schermata è disabilitato per proteggere il database.")

            st.markdown("---")

        if not st.session_state.db_clienti.empty:
            lista_completa = st.session_state.db_clienti['CLIENTE'].dropna().tolist()

            def aggiorna_selezione():
                st.session_state.clienti_selezionati_m = st.session_state.widget_multiselect

            clienti_selezionati = st.multiselect(
                "Cerca e seleziona i clienti per le tue consegne:",
                options=lista_completa,
                default=st.session_state.clienti_selezionati_m,
                key="widget_multiselect",
                on_change=aggiorna_selezione
            )

            if clienti_selezionati:
                st.markdown("<br>", unsafe_allow_html=True)
                st.markdown("### 📦 Configura Colli per i Clienti Selezionati")
                
                df_sel = st.session_state.db_clienti[st.session_state.db_clienti['CLIENTE'].isin(clienti_selezionati)].copy()
                df_sel['Q.ta'] = df_sel['QTA_DEFAULT']
                
                df_edit_colli = st.data_editor(
                    df_sel[['CLIENTE', 'COMUNE', 'Q.ta']],
                    hide_index=True,
                    use_container_width=True,
                    key="editor_colli_scelti"
                )

                if st.button("➕ CONFERMA E AGGIUNGI AL MIO GIRO", use_container_width=True, type="primary"):
                    nuovi_clienti = st.session_state.db_clienti[st.session_state.db_clienti['CLIENTE'].isin(clienti_selezionati)].copy()
                    
                    qta_dict = dict(zip(df_edit_colli['CLIENTE'], df_edit_colli['Q.ta']))
                    nuovi_clienti['Q.ta'] = nuovi_clienti['CLIENTE'].map(qta_dict)
                    
                    nuovi_clienti = nuovi_clienti[['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']] if 'POSIZIONE' in nuovi_clienti.columns else nuovi_clienti[['CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']]
                    nuovi_clienti['STATO'] = STATO_DA_FARE
                    
                    st.session_state.giro_corrente = pd.concat([st.session_state.giro_corrente, nuovi_clienti], ignore_index=True)
                    # Nuovo/nuovamente preparato giro: lo stato TERMINA GIRO precedente non vale piu'.
                    st.session_state.giro_terminato = False
                    st.session_state.inizio_giro_reale = None
                    st.session_state.fine_giro_reale = None
                    st.session_state.previsione_giro = None
                    st.session_state.metriche_giro_corrente = None
                    salva_stato_giro_persistente(st.session_state.utente_corrente)
                    st.session_state.giro_corrente['POSIZIONE'] = [str(i) for i in range(1, len(st.session_state.giro_corrente) + 1)]
                    
                    salva_giro_utente_su_sheets(st.session_state.utente_corrente, st.session_state.giro_corrente)
                    st.session_state.clienti_selezionati_m = []
                    
                    st.success("Clienti aggiunti al tuo giro e salvati su Google Sheets!")
                    st.session_state.pagina_attiva = "giro"
                    st.rerun()
                
            if st.session_state.is_admin:
                st.markdown("---")
                with st.expander("👀 Visualizza Anagrafica Clienti (sola lettura)"):
                    st.dataframe(
                        st.session_state.db_clienti,
                        hide_index=True,
                        use_container_width=True
                    )
                    st.caption("🔒 Foglio1 è protetto: nessuna modifica all'anagrafica. Solo la colonna H (COORDINATE) può essere aggiornata automaticamente.")
        else:
            st.warning("Nessun cliente trovato su Google Sheets.")

    # ==========================================
    # SCHERMATA 3: GESTIONE UTENTI (SOLO ADMIN)
    # ==========================================
    elif st.session_state.pagina_attiva == "utenti" and st.session_state.is_admin:
        st.subheader("🔑 Gestione Utenti da Google Sheets")
        st.markdown("<p style='color: #94A3B8; font-size: 14px;'>Gestisci gli account autorizzati direttamente dal foglio Google dedicato.</p>", unsafe_allow_html=True)

        dict_u = carica_utenti_da_sheets()
        df_utenti_attuali = pd.DataFrame(list(dict_u.items()), columns=["USERNAME", "PASSWORD"])

        st.dataframe(
            df_utenti_attuali,
            hide_index=True,
            use_container_width=True
        )
        st.info("🔒 La scheda Utenti è protetta e viene utilizzata esclusivamente in lettura dall'app.")
