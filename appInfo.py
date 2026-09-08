import streamlit as st
import pandas as pd
import urllib.parse
import os
import base64
import json
import time
from datetime import datetime
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None
import requests
from io import BytesIO
import gspread
from google.oauth2.service_account import Credentials

# Configurazione Pagina
st.set_page_config(
    page_title="VanGo - Giro Consegne",
    page_icon="🚐",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Stati consegna: definiti PRIMA di qualsiasi uso nel codice.
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


def _timestamp_oggi_alle_0520():
    """Timestamp locale Europe/Rome di oggi alle 05:20, usato come fallback."""
    try:
        tz = ZoneInfo("Europe/Rome") if ZoneInfo is not None else None
        adesso = datetime.now(tz) if tz else datetime.now()
        dt = adesso.replace(hour=5, minute=20, second=0, microsecond=0)
        return dt.timestamp()
    except Exception:
        adesso = datetime.now()
        return adesso.replace(hour=5, minute=20, second=0, microsecond=0).timestamp()


def _ora_partenza_reale_minuti():
    """Restituisce l'ora di partenza effettiva in minuti dalla mezzanotte.

    Se INIZIA GIRO e' stato premuto, usa il timestamp registrato.
    Altrimenti usa il fallback operativo delle 05:20.
    """
    timestamp = st.session_state.get("inizio_giro_reale")
    if timestamp is None:
        timestamp = _timestamp_oggi_alle_0520()
    try:
        tz = ZoneInfo("Europe/Rome") if ZoneInfo is not None else None
        dt = datetime.fromtimestamp(float(timestamp), tz) if tz else datetime.fromtimestamp(float(timestamp))
        return dt.hour * 60 + dt.minute + dt.second / 60.0
    except Exception:
        return 320.0


def _formatta_ora_partenza_reale():
    minuti = _ora_partenza_reale_minuti()
    return _formatta_ora_minuti(round(minuti))


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
    sh = client_gs.open("VanGo Database")
    
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
except Exception as e:
    st.error(f"⚠️ Errore di connessione a Google Sheets: {e}")
    sheet_db = None
    sheet_utenti = None
    sheet_giro = None

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
    return pd.DataFrame(columns=['UTENTE', 'POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta', 'STATO'])

def carica_giro_utente_da_sheets(nome_utente):
    cols_giro = ['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta', 'STATO']
    df_vuoto = pd.DataFrame(columns=cols_giro)
    try:
        df = carica_tutti_i_giri_da_sheets()
        if not df.empty:
            df.columns = df.columns.str.strip().str.upper()
            if 'UTENTE' not in df.columns:
                return df_vuoto
            
            df_utente = df[df['UTENTE'].astype(str).str.strip().str.lower() == nome_utente.strip().lower()].copy()
            
            if 'Q.TA' in df_utente.columns and 'Q.TA' not in cols_giro:
                df_utente = df_utente.rename(columns={'Q.TA': 'Q.ta'})
            
            for c in cols_giro:
                if c not in df_utente.columns:
                    df_utente[c] = ""
            
            df_utente = df_utente[cols_giro]
            if 'STATO' not in df_utente.columns:
                df_utente['STATO'] = ''
            df_utente['STATO'] = df_utente['STATO'].fillna('').astype(str)
            if not df_utente.empty and len(df_utente.dropna(how='all')) > 0:
                df_utente['POSIZIONE'] = [str(i) for i in range(1, len(df_utente) + 1)]
                return df_utente.reset_index(drop=True)
    except Exception as e:
        st.error(f"Errore di lettura del giro da Google Sheets: {e}")
    return df_vuoto

def salva_giro_utente_su_sheets(nome_utente, df_nuovo_giro):
    """Salva il giro esclusivamente su GiroAttivo.

    Foglio1 e Utenti non vengono mai modificati da questa funzione.
    Le eventuali righe tecniche di backup presenti in GiroAttivo vengono mantenute.
    """
    cols_ordine = ['UTENTE', 'POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta', 'STATO', 'TIPO_RIGA', 'BACKUP_JSON']
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

                sheet_giro.clear()
                if df_tutti.empty:
                    sheet_giro.update([cols_ordine])
                else:
                    data_to_update = [cols_ordine] + df_tutti.astype(str).values.tolist()
                    sheet_giro.update(data_to_update)

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


BACKUP_UTENTE_PREFIX = "__VANGO_BACKUP__::"
GIRO_META_PREFIX = "__VANGO_META__::"

def _meta_utente_giro(nome_utente):
    return f"{GIRO_META_PREFIX}{str(nome_utente).strip()}"

def carica_stato_giro_persistente(nome_utente):
    """Legge lo stato tecnico del giro da GiroAttivo, senza modificare Foglio1/Utenti."""
    risultato = {"giro_terminato": False, "inizio_giro_reale": None, "fine_giro_reale": None, "previsione_giro": None}
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
    except Exception:
        pass
    return risultato

def salva_stato_giro_persistente(nome_utente):
    """Memorizza lo stato di TERMINA GIRO dentro GiroAttivo."""
    import json as _json
    meta = {
        "tipo": "STATO_GIRO",
        "giro_terminato": bool(st.session_state.get("giro_terminato", False)),
        "inizio_giro_reale": st.session_state.get("inizio_giro_reale"),
        "fine_giro_reale": st.session_state.get("fine_giro_reale"),
        "previsione_giro": st.session_state.get("previsione_giro"),
    }
    payload = _json.dumps(meta, ensure_ascii=False)
    cols_ordine = ['UTENTE', 'POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta', 'STATO', 'TIPO_RIGA', 'BACKUP_JSON']
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
                sheet_giro.clear()
                sheet_giro.update([cols_ordine] + df_tutti.astype(str).values.tolist())
                st.cache_data.clear()
                return True
        except Exception:
            if tentativo == 4:
                return False
    return False

def salva_stato_consegna(idx, stato):
    """Aggiorna solo lo stato della consegna e lo salva su GiroAttivo."""
    df = st.session_state.giro_corrente.copy()
    if df.empty or idx < 0 or idx >= len(df):
        return
    if 'STATO' not in df.columns:
        df['STATO'] = STATO_DA_FARE
    df.at[idx, 'STATO'] = stato
    st.session_state.giro_corrente = df
    st.session_state.fine_giro_reale = None
    st.session_state.giro_terminato = False
    salva_stato_giro_persistente(st.session_state.utente_corrente)
    salva_giro_utente_su_sheets(st.session_state.utente_corrente, df)
    st.rerun()

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
        riga = {str(k): v for k, v in riga.items()}
        riga["__VANGO_POSIZIONE_BACKUP"] = posizione
        snapshot.append(riga)
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
    """Salva l'ordine corrente in una riga tecnica di GiroAttivo."""
    df = st.session_state.giro_corrente.copy()
    if df.empty or not st.session_state.utente_corrente:
        return False
    snapshot = _crea_snapshot_ordine(df)
    payload = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':'))
    backup_utente = BACKUP_UTENTE_PREFIX + str(st.session_state.utente_corrente).strip()

    for tentativo in range(5):
        try:
            if not sheet_giro:
                return False
            time.sleep(1.5 * (tentativo + 1))
            data = sheet_giro.get_all_records()
            df_all = pd.DataFrame(data) if data else pd.DataFrame(columns=['UTENTE','POSIZIONE','CLIENTE','COMUNE','VIA','ORA','Q.ta','STATO','TIPO_RIGA','BACKUP_JSON'])
            df_all.columns = [str(c).strip() for c in df_all.columns]
            for c in ['UTENTE','POSIZIONE','CLIENTE','COMUNE','VIA','ORA','Q.ta','STATO','TIPO_RIGA','BACKUP_JSON']:
                if c not in df_all.columns:
                    df_all[c] = ''
            df_all = df_all[['UTENTE','POSIZIONE','CLIENTE','COMUNE','VIA','ORA','Q.ta','STATO','TIPO_RIGA','BACKUP_JSON']]
            df_all = df_all[df_all['UTENTE'].astype(str) != backup_utente].copy()
            nuova = pd.DataFrame([{
                'UTENTE': backup_utente, 'POSIZIONE': str(st.session_state.utente_corrente),
                'CLIENTE': 'BACKUP POSIZIONE GIRO', 'COMUNE': '', 'VIA': '', 'ORA': '', 'Q.ta': '', 'STATO': '',
                'TIPO_RIGA': 'BACKUP_POSIZIONE', 'BACKUP_JSON': payload
            }])
            df_all = pd.concat([df_all, nuova], ignore_index=True)
            sheet_giro.clear()
            sheet_giro.update([['UTENTE','POSIZIONE','CLIENTE','COMUNE','VIA','ORA','Q.ta','STATO','TIPO_RIGA','BACKUP_JSON']] + df_all.astype(str).values.tolist())
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
        stato_persistente = carica_stato_giro_persistente(st.session_state.utente_corrente)
        st.session_state.giro_terminato = bool(stato_persistente.get("giro_terminato", False))
        st.session_state.inizio_giro_reale = stato_persistente.get("inizio_giro_reale")
        st.session_state.fine_giro_reale = stato_persistente.get("fine_giro_reale")
        st.session_state.previsione_giro = stato_persistente.get("previsione_giro")
        st.session_state.metriche_giro_corrente = None
        st.session_state.ultimo_utente_caricato = st.session_state.utente_corrente
    else:
        st.session_state.giro_corrente = pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta', 'STATO'])
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
        st.markdown(f"""
        <div class="logo-container">
            <img src="data:image/png;base64,{encoded_icon}" alt="VanGo Logo">
        </div>
        """, unsafe_allow_html=True)
    
    st.markdown("<h2 style='text-align: center; color: #FFFFFF; margin-bottom: 20px;'>Benvenuto in VanGo</h2>", unsafe_allow_html=True)
    
    with st.form("form_login"):
        username_input = st.text_input("Username").strip()
        password_input = st.text_input("Password", type="password").strip()
        ricordami_input = st.checkbox("Ricordami su questo dispositivo", value=True)
        
        submitted = st.form_submit_button("Accedi", use_container_width=True, type="primary")
        
        if submitted:
            utenti_validi = st.session_state.utenti_sistema
            usr_clean = username_input.lower()
            
            # Cerca utente case-insensitive nel dizionario
            match_usr = None
            for u in utenti_validi:
                if u.lower() == usr_clean:
                    match_usr = u
                    break
                    
            if match_usr and utenti_validi[match_usr] == password_input:
                st.session_state.autenticato = True
                st.session_state.utente_corrente = match_usr
                st.session_state.is_admin = (match_usr.lower() == "admin")
                st.session_state.pagina_attiva = "giro"
                st.session_state.ricordami_attivo = ricordami_input
                
                if ricordami_input:
                    salva_sessione_persistente(match_usr)
                    
                st.success("Accesso effettuato con successo!")
                time.sleep(0.5)
                st.rerun()
            else:
                st.error("Credenziali non valide. Riprova.")

# ==========================================
# APPLICAZIONE PRINCIPALE (AUTENTICATO)
# ==========================================
elif st.session_state.autenticato:
    
    # Menu di Navigazione in Alto
    nav_cols = st.columns(3)
    
    with nav_cols[0]:
        btn_giro = st.button("🚐 Giro Consegne", use_container_width=True, type="primary" if st.session_state.pagina_attiva == "giro" else "secondary")
        if btn_giro:
            st.session_state.pagina_attiva = "giro"
            st.rerun()
            
    with nav_cols[1]:
        btn_db = st.button("📋 Database Clienti", use_container_width=True, type="primary" if st.session_state.pagina_attiva == "db" else "secondary")
        if btn_db:
            st.session_state.pagina_attiva = "db"
            st.rerun()
            
    with nav_cols[2]:
        btn_logout = st.button("🚪 Logout", use_container_width=True)
        if btn_logout:
            elimina_sessione_persistente()
            st.session_state.autenticato = False
            st.session_state.utente_corrente = ""
            st.session_state.is_admin = False
            st.session_state.pagina_attiva = "welcome"
            st.rerun()

    st.markdown("---")

    # ----------------------------------------------------
    # PAGINA 1: DATABASE CLIENTI (ADMIN / GESTIONE)
    # ----------------------------------------------------
    if st.session_state.pagina_attiva == "db":
        st.markdown("### 📋 Gestione Database Clienti (Foglio1)")
        
        # Pulsante per geolocalizzare l'intero database in un sol colpo
        col_geo1, col_geo2 = st.columns([2, 1])
        with col_geo1:
            st.info("💡 La geolocalizzazione automatica interroga OpenStreetMap per calcolare le coordinate di tutti i clienti privi di posizione.")
        with col_geo2:
            if st.button("🌍 Geolocalizza Tutti i Clienti", use_container_width=True, type="primary"):
                with st.spinner("Geolocalizzazione in corso..."):
                    db_aggiornato, trovati, gia_presenti, non_trovati = geolocalizza_tutti_clienti(
                        st.session_state.db_clienti,
                        salvataggio_progressivo=lambda df: salva_coordinate_su_google_sheets(df)
                    )
                    st.session_state.db_clienti = db_aggiornato
                    salva_coordinate_su_google_sheets(db_aggiornato)
                    st.success(f"Completato! Trovate {trovati} nuove coordinate (già presenti: {gia_presenti}).")
                    if non_trovati:
                        st.warning(f"Indirizzi non geolocalizzabili ({len(non_trovati)}): " + ", ".join(non_trovati[:5]))
                    time.sleep(1)
                    st.rerun()

        df_db_view = st.session_state.db_clienti.copy()
        
        # Mostra tabella interattiva dei clienti
        st.dataframe(df_db_view, use_container_width=True, hide_index=True)

    # ----------------------------------------------------
    # PAGINA 2: GIRO CONSEGNE (OPERATIVO AUTISTA)
    # ----------------------------------------------------
    elif st.session_state.pagina_attiva == "giro":
        
        # Titolo e stato utente
        st.markdown(f"### 🚐 Giro Consegne — Utente: **{st.session_state.utente_corrente.upper()}**")
        
        df_giro = st.session_state.giro_corrente
        
        if df_giro.empty:
            st.info("📭 Nessun giro attivo caricato per questo utente. Contatta l'amministratore o importa un giro.")
        else:
            # Pulsanti di controllo rapido del giro
            col_ctrl1, col_ctrl2, col_ctrl3 = st.columns(3)
            
            with col_ctrl1:
                if not st.session_state.get("giro_terminato", False):
                    if st.button("🏁 Termina Giro", use_container_width=True, type="primary"):
                        st.session_state.giro_terminato = True
                        st.session_state.fine_giro_reale = time.time()
                        salva_stato_giro_persistente(st.session_state.utente_corrente)
                        st.success("Giro completato!")
                        st.rerun()
                else:
                    if st.button("🔄 Riattiva Giro", use_container_width=True):
                        st.session_state.giro_terminato = False
                        st.session_state.fine_giro_reale = None
                        salva_stato_giro_persistente(st.session_state.utente_corrente)
                        st.rerun()
                        
            with col_ctrl2:
                if st.button("💾 Salva Posizione", use_container_width=True):
                    if salva_posizione_giro():
                        st.success("Posizione salvata con successo!")
                    else:
                        st.error("Errore durante il salvataggio.")
                        
            with col_ctrl3:
                if st.button("♻️ Ripristina Backup", use_container_width=True):
                    if ripristina_posizione_giro():
                        st.success("Giro ripristinato dal backup!")
                        time.sleep(0.5)
                        st.rerun()
                    else:
                        st.warning("Nessun backup disponibile per il ripristino.")

            st.markdown("---")

            # Visualizzazione delle fermate del giro
            vista_operative = prepara_vista_giro(df_giro)
            
            for idx, row in vista_operative.iterrows():
                idx_reale = row.get("__IDX_ORIGINALE", idx)
                cliente = row.get("CLIENTE", "Cliente")
                comune = row.get("COMUNE", "")
                via = row.get("VIA", "")
                ora = row.get("ORA", "")
                qta = row.get("Q.ta", "0")
                stato = row.get("STATO", STATO_DA_FARE)
                
                with st.container():
                    st.markdown(f"""
                    <div class="stop-card">
                        <div class="stop-title">{row.get('POSIZIONE', idx+1)}. {cliente}</div>
                        <div class="stop-address">📍 {via}, {comune}</div>
                        <div class="stop-meta">🕒 Orario: {ora if ora else 'Flessibile'} | 📦 Q.ta: {qta} | Stato: {stato}</div>
                    </div>
                    """, unsafe_allow_html=True)
                    
                    # Pulsanti interattivi per cambiare lo stato della consegna direttamente sulla card
                    cols_stato = st.columns(4)
                    with cols_stato[0]:
                        if st.button("⚪ Da Fare", key=f"df_{idx_reale}", use_container_width=True):
                            salva_stato_consegna(idx_reale, STATO_DA_FARE)
                    with cols_stato[1]:
                        if st.button("🟢 Fatto", key=f"ft_{idx_reale}", use_container_width=True):
                            salva_stato_consegna(idx_reale, STATO_FATTO)
                    with cols_stato[2]:
                        if st.button("🟡 Parz.", key=f"pr_{idx_reale}", use_container_width=True):
                            salva_stato_consegna(idx_reale, STATO_PARZIALE)
                    with cols_stato[3]:
                        if st.button("🔴 Resp.", key=f"rs_{idx_reale}", use_container_width=True):
                            salva_stato_consegna(idx_reale, STATO_RESPINTO)
