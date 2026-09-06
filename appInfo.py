import streamlit as st
import pandas as pd
import urllib.parse
import os
import base64
import time
import requests
import gspread
from google.oauth2.service_account import Credentials

# Configurazione Pagina
st.set_page_config(
    page_title="VanGo - Giro Consegne",
    page_icon="🚐",
    layout="wide",
    initial_sidebar_state="collapsed"
)

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


def _ottimizza_a_blocchi_zona(distanze, durate, n_clienti, gruppi):
    """Genera più percorsi a blocchi ZONA e restituisce il migliore su strada."""
    gruppi_validi = sorted({gruppi[i] for i in range(1, n_clienti + 1)
                             if i < len(gruppi) and gruppi[i] is not None})
    if not gruppi_validi:
        return [0] + list(range(1, n_clienti + 1)) + [0]

    membri = {g: [i for i in range(1, n_clienti + 1) if gruppi[i] == g] for g in gruppi_validi}

    # Generiamo più ordini di macro-ZONE. Tutti mantengono le ZONE in blocchi;
    # cambia solo quale blocco viene visitato prima.
    sequenze = []

    # 1) greedy partendo dal deposito, scegliendo la ZONA con l'ingresso più vicino
    rimanenti = set(gruppi_validi)
    corrente = 0
    seq = []
    while rimanenti:
        g = min(rimanenti, key=lambda z: min(_costo_arco_base(corrente, j, distanze, durate) for j in membri[z]))
        seq.append(g)
        # Per il passo successivo usiamo la fermata del gruppo più vicina come proxy di uscita.
        corrente = min(membri[g], key=lambda j: _costo_arco_base(0 if len(seq) == 1 else corrente, j, distanze, durate))
        rimanenti.remove(g)
    sequenze.append(seq)
    sequenze.append(list(reversed(seq)))

    # 2) ordine per vicinanza al deposito (utile quando le ZONE sono geograficamente distribuite)
    seq_dep = sorted(gruppi_validi, key=lambda g: min(_costo_arco_base(0, j, distanze, durate) for j in membri[g]))
    sequenze.append(seq_dep)
    sequenze.append(list(reversed(seq_dep)))

    # 3) ordine numerico crescente/decrescente: candidato deterministico di sicurezza.
    sequenze.append(sorted(gruppi_validi))
    sequenze.append(sorted(gruppi_validi, reverse=True))

    candidati = []
    viste = set()
    for seq in sequenze:
        chiave = tuple(seq)
        if chiave in viste:
            continue
        viste.add(chiave)
        ordine = _ordine_blocchi_da_sequenza_gruppi(distanze, durate, gruppi, seq)
        costo = _costo_base_ordine(ordine, distanze, durate)
        candidati.append((costo, ordine))

    return min(candidati, key=lambda x: x[0])[1]

def ottimizza_giro_free(df_giro, df_db=None, forza_gruppamento_zona=75):
    """Ottimizza il giro su strada con una seconda priorita' REALE per ZONA.

    0%  = solo strada.
    100% = blocchi ZONA obbligatori come strategia di ordinamento (la strada
           continua a decidere l'ordine interno al blocco).
    Valori intermedi = compromesso tra percorso stradale e blocchi ZONA.
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
            distanze, durate, len(df_originale), gruppi
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
    elif forza_gruppamento_zona >= 95:
        # 100% non puo' restare uguale per colpa di una normalizzazione:
        # scegliamo esplicitamente il candidato a blocchi.
        nome_scelto, ordine_ottimizzato = ("BLOCCHI ZONA", ordine_blocchi)
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
    try:
        if sheet_utenti:
            time.sleep(1.0)
            sheet_utenti.clear()
            data_to_update = [["USERNAME", "PASSWORD"]] + [[u, p] for u, p in dict_utenti.items()]
            sheet_utenti.update(data_to_update)
            st.cache_data.clear()
    except Exception as e:
        st.error(f"Errore nel salvataggio utenti su Google Sheets: {e}")

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
    try:
        if sheet_db:
            time.sleep(1.0)
            sheet_db.clear()
            data_to_update = [df.columns.values.tolist()] + df.astype(str).values.tolist()
            sheet_db.update(data_to_update)
            st.cache_data.clear()
    except Exception as e:
        st.error(f"Errore nel salvataggio su Google Sheets: {e}")

# Database Clienti con TTL ottimizzato a 300s
@st.cache_data(ttl=300, show_spinner=False)
def carica_db_da_google_sheets_cached():
    try:
        if sheet_db:
            valori_grezzi = sheet_db.get_all_values()
            if not valori_grezzi:
                intestazioni_default = ['POSIZIONE', 'ZONA', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'QTA_DEFAULT', 'COORDINATE']
                sheet_db.update([intestazioni_default])
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
    return pd.DataFrame(columns=['UTENTE', 'POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])

def carica_giro_utente_da_sheets(nome_utente):
    cols_giro = ['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']
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
            if not df_utente.empty and len(df_utente.dropna(how='all')) > 0:
                df_utente['POSIZIONE'] = [str(i) for i in range(1, len(df_utente) + 1)]
                return df_utente.reset_index(drop=True)
    except Exception as e:
        st.error(f"Errore di lettura del giro da Google Sheets: {e}")
    return df_vuoto

def salva_giro_utente_su_sheets(nome_utente, df_nuovo_giro):
    for tentativo in range(5):
        try:
            if sheet_giro:
                time.sleep(1.5 * (tentativo + 1))
                
                data_totale = sheet_giro.get_all_records()
                df_tutti = pd.DataFrame(data_totale) if data_totale else pd.DataFrame(columns=['UTENTE', 'POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])
                
                if not df_tutti.empty:
                    df_tutti.columns = df_tutti.columns.str.strip().str.upper()
                    if 'Q.TA' in df_tutti.columns:
                        df_tutti = df_tutti.rename(columns={'Q.TA': 'Q.ta'})
                    df_tutti = df_tutti[df_tutti['UTENTE'].astype(str).str.strip().str.lower() != nome_utente.strip().lower()]
                
                if not df_nuovo_giro.empty:
                    df_agg = df_nuovo_giro.copy()
                    df_agg['UTENTE'] = nome_utente
                    df_agg['POSIZIONE'] = range(1, len(df_agg) + 1)
                    cols_ordine = ['UTENTE', 'POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']
                    for c in cols_ordine:
                        if c not in df_agg.columns:
                            df_agg[c] = ""
                    df_agg = df_agg[cols_ordine]
                    
                    if df_tutti.empty:
                        df_tutti = df_agg
                    else:
                        for c in cols_ordine:
                            if c not in df_tutti.columns:
                                df_tutti[c] = ""
                        df_tutti = pd.concat([df_tutti[cols_ordine], df_agg[cols_ordine]], ignore_index=True)
                
                sheet_giro.clear()
                intestazioni = ['UTENTE', 'POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']
                if df_tutti.empty:
                    sheet_giro.update([intestazioni])
                else:
                    data_to_update = [intestazioni] + df_tutti.astype(str).values.tolist()
                    sheet_giro.update(data_to_update)
                
                st.cache_data.clear()
                return
        except Exception as e:
            if "429" in str(e) and tentativo < 4:
                continue
            elif tentativo == 4:
                st.error(f"Errore nel salvataggio del giro su Google Sheets dopo vari tentativi: {e}")
            else:
                st.error(f"Errore nel salvataggio del giro su Google Sheets: {e}")
                break

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
        st.session_state.ultimo_utente_caricato = st.session_state.utente_corrente
    else:
        st.session_state.giro_corrente = pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])

if 'clienti_selezionati_m' not in st.session_state:
    st.session_state.clienti_selezionati_m = []

if 'vista_pulita' not in st.session_state:
    st.session_state.vista_pulita = False

if 'forza_gruppamento_zona' not in st.session_state:
    st.session_state.forza_gruppamento_zona = 75

if 'giro_ottimizzato_proposto' not in st.session_state:
    st.session_state.giro_ottimizzato_proposto = None

if 'metriche_ottimizzazione' not in st.session_state:
    st.session_state.metriche_ottimizzazione = None

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
        padding: 12px 16px;
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
        st.markdown(f"<p style='color: #94A3B8; font-size: 13px; margin: 0;'>👤 Utente: <b style='color: #60A5FA;'>{st.session_state.get('utente_corrente', '')}</b></p>", unsafe_allow_html=True)
    with col_logout_u:
        if st.button("🚪 LOGOUT", use_container_width=True, key="btn_logout_principale"):
            elimina_sessione_persistente()
            st.session_state.autenticato = False
            st.session_state.utente_corrente = ""
            st.session_state.is_admin = False
            st.session_state.pagina_attiva = "login"
            st.rerun()

    st.markdown("<div style='margin-bottom: 10px;'></div>", unsafe_allow_html=True)

    if st.session_state.is_admin:
        col_sw1, col_sw2, col_sw3 = st.columns(3)
    else:
        col_sw1, col_sw2 = st.columns(2)

    with col_sw1:
        css_class = "btn-active" if st.session_state.pagina_attiva == "giro" else "btn-inactive"
        st.markdown(f'<div class="{css_class}">', unsafe_allow_html=True)
        if st.button("📍 GIRO", use_container_width=True, key="btn_giro"):
            st.session_state.pagina_attiva = "giro"
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    with col_sw2:
        css_class = "btn-active" if st.session_state.pagina_attiva == "db" else "btn-inactive"
        st.markdown(f'<div class="{css_class}">', unsafe_allow_html=True)
        if st.button("📁 CLIENTI", use_container_width=True, key="btn_db"):
            st.session_state.pagina_attiva = "db"
            st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    if st.session_state.is_admin:
        with col_sw3:
            css_class = "btn-active" if st.session_state.pagina_attiva == "utenti" else "btn-inactive"
            st.markdown(f'<div class="{css_class}">', unsafe_allow_html=True)
            if st.button("🔑 UTENTI", use_container_width=True, key="btn_utenti"):
                st.session_state.pagina_attiva = "utenti"
                st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)

    st.session_state.forza_gruppamento_zona = st.slider(
        "🎯 Forza raggruppamento ZONA",
        min_value=0,
        max_value=100,
        value=int(st.session_state.forza_gruppamento_zona),
        step=5,
        help="0% = ZONA ignorata. 100% = forte preferenza a completare un gruppo prima di passare al successivo. Non e' un vincolo rigido.",
    )
    if st.session_state.forza_gruppamento_zona == 0:
        st.caption("Forza attuale: **0%** — ZONA completamente ignorata: ottimizzo solo la strada.")
    elif st.session_state.forza_gruppamento_zona >= 95:
        st.caption(f"Forza attuale: **{st.session_state.forza_gruppamento_zona}%** — massima priorita' ai blocchi ZONA.")
    else:
        st.caption(f"Forza attuale: **{st.session_state.forza_gruppamento_zona}%** — compromesso tra strada e raggruppamento ZONA.")

    col_act1, col_act2, col_act3 = st.columns(3)

    with col_act1:
        st.markdown('<div class="btn-inactive">', unsafe_allow_html=True)
        if st.button("🔄 INVERTI SEQUENZA", use_container_width=True, key="btn_inverti"):
            if not st.session_state.giro_corrente.empty:
                st.session_state.giro_corrente = st.session_state.giro_corrente.iloc[::-1].reset_index(drop=True)
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
                st.session_state.giro_corrente = pd.DataFrame(columns=['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta'])
                salva_giro_utente_su_sheets(st.session_state.utente_corrente, st.session_state.giro_corrente)
                st.session_state.giro_ottimizzato_proposto = None
                st.session_state.metriche_ottimizzazione = None
                st.rerun()
        st.markdown('</div>', unsafe_allow_html=True)

    with col_act3:
        st.markdown('<div class="btn-inactive">', unsafe_allow_html=True)
        if st.button("🧠 OTTIMIZZA GIRO", use_container_width=True, key="btn_ottimizza"):
            if st.session_state.giro_corrente.empty:
                st.warning("⚠️ Il giro è vuoto.")
            elif len(st.session_state.giro_corrente) < 2:
                st.info("ℹ️ Servono almeno 2 fermate per ottimizzare il giro.")
            else:
                try:
                    with st.spinner("🧠 Analizzo indirizzi e percorso stradale..."):
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
                        salva_db_su_google_sheets(st.session_state.db_clienti)
                    st.session_state.giro_ottimizzato_proposto = df_opt
                    st.session_state.metriche_ottimizzazione = metriche_opt
                    st.success("Giro ottimizzato pronto: controllalo e poi scegli se applicarlo.")
                    st.rerun()
                except Exception as e:
                    st.error(f"❌ Ottimizzazione non riuscita: {e}")
        st.markdown('</div>', unsafe_allow_html=True)

    st.markdown("<div style='margin-bottom: 5px;'></div>", unsafe_allow_html=True)

    # ==========================================
    # ANTEPRIMA GIRO OTTIMIZZATO
    # ==========================================
    if st.session_state.giro_ottimizzato_proposto is not None:
        df_proposto = st.session_state.giro_ottimizzato_proposto
        m = st.session_state.metriche_ottimizzazione or {}
        st.markdown("---")
        st.subheader("🧠 Anteprima percorso ottimizzato")
        st.caption("Start e fine giro: Dolciaria Acquaviva — Via Enrico Fermi 10, Burago di Molgora. Il campo ORA non viene usato per l'ottimizzazione.")
        st.info(f"🎯 Forza raggruppamento ZONA usata: **{m.get('forza_gruppamento_zona', st.session_state.forza_gruppamento_zona)}%**")
        seq_zona = m.get("sequenza_zona", [])
        if seq_zona:
            st.caption(f"🗺️ Sequenza ZONA: **{' → '.join(map(str, seq_zona))}**  |  Cambi ZONA: **{m.get('cambi_zona', 0)}**  |  Rientri: **{m.get('rientri_zona', 0)}**")
            st.caption(f"📦 Macro-ZONE trovate: **{m.get('gruppi_zona', 0)}** — Metodo scelto: **{m.get('metodo', '')}**")
        elif m.get("gruppi_zona", 0) == 0:
            st.warning("⚠️ Nessuna ZONA disponibile per le fermate di questo giro: la percentuale non può influire sul percorso.")

        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Km", f"{m.get('km_ottimizzati', 0):.1f}", delta=f"{m.get('risparmio_km', 0):+.1f} km")
        c2.metric("Tempo strada", f"{m.get('min_ottimizzati', 0):.0f} min", delta=f"{m.get('risparmio_min', 0):+.0f} min")
        c3.metric("Fermate", f"{m.get('fermate', len(df_proposto))}")
        c4.metric("Metodo", "FREE")

        st.dataframe(
            df_proposto[['POSIZIONE', 'CLIENTE', 'COMUNE', 'VIA', 'ORA', 'Q.ta']],
            hide_index=True,
            use_container_width=True,
        )

        col_applica, col_annulla = st.columns(2)
        with col_applica:
            if st.button("✅ APPLICA GIRO OTTIMIZZATO", use_container_width=True, type="primary", key="btn_applica_ottimizzato"):
                st.session_state.giro_corrente = df_proposto.copy()
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

        col_m1, col_m2, col_m3 = st.columns(3)
        col_m1.metric("Fermate Totali", f"{tot_clienti}")
        col_m2.metric("Pezzi Totali", f"{tot_qta}")
        col_m3.metric("Comuni", f"{tot_comuni}")

        st.markdown("---")

        if not st.session_state.giro_corrente.empty:
            label_btn_vista = "👁️ TORNA ALLA VISTA OPERATIVA" if st.session_state.vista_pulita else "📋 VISTA RIEPILOGO PULITA"
            if st.button(label_btn_vista, use_container_width=True):
                st.session_state.vista_pulita = not st.session_state.vista_pulita
                st.rerun()
            st.markdown("<div style='margin-bottom: 10px;'></div>", unsafe_allow_html=True)

        if not st.session_state.giro_corrente.empty:
            st.session_state.giro_corrente['POSIZIONE'] = [str(i) for i in range(1, len(st.session_state.giro_corrente) + 1)]
            
            addresses = [f"{r['VIA']}, {r['COMUNE']}" for _, r in st.session_state.giro_corrente.iterrows()]
            if len(addresses) == 1:
                maps_url = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(addresses[0])}"
            else:
                origin = urllib.parse.quote(addresses[0])
                destination = urllib.parse.quote(addresses[-1])
                
                if len(addresses) > 2:
                    waypoints = "/".join([urllib.parse.quote(a) for a in addresses[1:-1]])
                    maps_url = f"https://www.google.com/maps/dir/{origin}/{waypoints}/{destination}"
                else:
                    maps_url = f"https://www.google.com/maps/dir/{origin}/{destination}"

            if st.session_state.vista_pulita:
                st.markdown(f"<p style='color: #94A3B8; font-size: 14px; margin-bottom: 15px;'>{tot_clienti} indirizzi trovati nel giro.</p>", unsafe_allow_html=True)
                
                for idx in range(tot_clienti):
                    row = st.session_state.giro_corrente.iloc[idx]
                    st.markdown(f"""
                    <div class="clean-card">
                        <div class="clean-badge">{idx + 1}</div>
                        <div class="clean-content">
                            <div class="clean-title">{row['VIA']}</div>
                            <div class="clean-subtitle">{row['COMUNE']} — Cliente: {row['CLIENTE']} (🕒 {row['ORA']} | 📦 {row['Q.ta']} pz)</div>
                        </div>
                    </div>
                    """, unsafe_allow_html=True)

                st.markdown("---")
                st.markdown(f'''
                    <a href="{maps_url}" target="_blank" style="text-decoration:none;">
                        <button style="width:100%; background-color:#2563EB; color:white; border:none; border-radius:25px; height:52px; font-weight:bold; font-size:16px; box-shadow: 0 4px 10px rgba(37, 99, 235, 0.4);">
                            🗺️ AVVIA PERCORSO
                        </button>
                    </a>
                ''', unsafe_allow_html=True)
            else:
                for idx in range(tot_clienti):
                    row = st.session_state.giro_corrente.iloc[idx]
                    
                    st.markdown(f"""
                    <div class="stop-card">
                        <div class="stop-title">{idx + 1}. {row['CLIENTE']}</div>
                        <div class="stop-address">📍 {row['VIA']}, {row['COMUNE']}</div>
                        <div class="stop-meta">🕒 Ora: {row['ORA']} | 📦 Q.tà: {row['Q.ta']} pz</div>
                    </div>
                    """, unsafe_allow_html=True)

                    col_c1, col_c2, col_c3 = st.columns([1, 1, 1])
                    
                    with col_c1:
                        dest = urllib.parse.quote(f"{row['VIA']}, {row['COMUNE']}")
                        st.write("")
                        st.markdown(f"[🚘 **NAVIGA ORA**](https://www.google.com/maps/dir/?api=1&destination={dest})")

                    with col_c2:
                        nuova_qta = st.number_input(
                            "Q.tà colli",
                            min_value=0,
                            value=int(row['Q.ta']),
                            key=f"qta_mobile_{row['CLIENTE']}_{idx}"
                        )
                        if nuova_qta != int(row['Q.ta']):
                            st.session_state.giro_corrente.at[idx, 'Q.ta'] = nuova_qta
                            salva_giro_utente_su_sheets(st.session_state.utente_corrente, st.session_state.giro_corrente)
                            st.rerun()

                    with col_c3:
                        nuova_pos = st.selectbox(
                            "Sposta a pos:",
                            options=[i for i in range(1, tot_clienti + 1)],
                            index=idx,
                            key=f"select_pos_{row['CLIENTE']}_{idx}"
                        )
                        
                        if nuova_pos - 1 != idx:
                            df_temp = st.session_state.giro_corrente.copy()
                            riga = df_temp.iloc[idx]
                            df_temp = df_temp.drop(df_temp.index[idx])
                            top = df_temp.iloc[:nuova_pos - 1]
                            bottom = df_temp.iloc[nuova_pos - 1:]
                            
                            df_nuovo = pd.concat([top, pd.DataFrame([riga]), bottom], ignore_index=True)
                            df_nuovo['POSIZIONE'] = [str(i) for i in range(1, len(df_nuovo) + 1)]
                            
                            st.session_state.giro_corrente = df_nuovo
                            salva_giro_utente_su_sheets(st.session_state.utente_corrente, st.session_state.giro_corrente)
                            st.rerun()

                    st.markdown("<hr style='margin: 10px 0; border-color: #262626;'>", unsafe_allow_html=True)

                st.markdown("---")
                st.markdown(f'''
                    <a href="{maps_url}" target="_blank" style="text-decoration:none;">
                        <button style="width:100%; background-color:#2563EB; color:white; border:none; border-radius:25px; height:52px; font-weight:bold; font-size:16px; box-shadow: 0 4px 10px rgba(37, 99, 235, 0.4);">
                            🗺️ AVVIA PERCORSO
                        </button>
                    </a>
                ''', unsafe_allow_html=True)
        else:
            st.info("Nessuna fermata nel tuo giro corrente. Clicca in alto su '📁 CLIENTI' per aggiungerne.")

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
            caricamento_file = st.file_uploader("Carica Database Clienti su Google Sheets (Excel o CSV)", type=["xlsx", "csv"])
            
            if caricamento_file is not None:
                try:
                    if caricamento_file.name.endswith('.csv'):
                        df_up = pd.read_csv(caricamento_file)
                    else:
                        df_up = pd.read_excel(caricamento_file)
                    
                    st.session_state.db_clienti = elabora_dataframe_db(df_up)
                    salva_db_su_google_sheets(st.session_state.db_clienti)
                    st.session_state.clienti_selezionati_m = []
                    
                    st.success(f"Database caricato e sincronizzato su Google Sheets! ({len(st.session_state.db_clienti)} clienti)")
                    st.rerun()
                except Exception as e:
                    st.error(f"Errore nel caricamento del file: {e}")
            
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
                    
                    st.session_state.giro_corrente = pd.concat([st.session_state.giro_corrente, nuovi_clienti], ignore_index=True)
                    st.session_state.giro_corrente['POSIZIONE'] = [str(i) for i in range(1, len(st.session_state.giro_corrente) + 1)]
                    
                    salva_giro_utente_su_sheets(st.session_state.utente_corrente, st.session_state.giro_corrente)
                    st.session_state.clienti_selezionati_m = []
                    
                    st.success("Clienti aggiunti al tuo giro e salvati su Google Sheets!")
                    st.session_state.pagina_attiva = "giro"
                    st.rerun()
                
            if st.session_state.is_admin:
                st.markdown("---")
                with st.expander("👀 Visualizza o Modifica Anagrafica Clienti intera"):
                    edited_db = st.data_editor(
                        st.session_state.db_clienti,
                        num_rows="dynamic",
                        use_container_width=True,
                        key="db_editor_switch"
                    )
                    if not edited_db.equals(st.session_state.db_clienti):
                        st.session_state.db_clienti = elabora_dataframe_db(edited_db)
                        salva_db_su_google_sheets(st.session_state.db_clienti)
                        st.rerun()
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

        edited_utenti = st.data_editor(
            df_utenti_attuali,
            num_rows="dynamic",
            use_container_width=True,
            key="editor_utenti_sheets"
        )

        if st.button("💾 SALVA MODIFICHE UTENTI SU GOOGLE SHEETS", use_container_width=True, type="primary"):
            nuovo_dict = {}
            for _, row in edited_utenti.iterrows():
                u = str(row["USERNAME"]).strip()
                p = str(row["PASSWORD"]).strip()
                if u and u.lower() != "nan":
                    nuovo_dict[u] = p
            
            if "admin" not in nuovo_dict:
                nuovo_dict["admin"] = "vango2026"

            salva_utenti_su_sheets(nuovo_dict)
            st.session_state.utenti_sistema = nuovo_dict
            st.success("Tabella utenti aggiornata e salvata su Google Sheets con successo!")
            st.rerun()
