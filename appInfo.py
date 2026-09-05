import streamlit as st
import pandas as pd
import gspread
from google.oauth2.service_account import Credentials

# Configurazione della pagina
st.set_page_config(
    page_title="VanGo - Gestione Giri e Consegne",
    page_icon="🚐",
    layout="wide"
)

# ---------------------------------------------------------
# CONNESSIONE A GOOGLE SHEETS
# ---------------------------------------------------------
@st.cache_resource
def init_connection():
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds_dict = dict(st.secrets["gcp_service_account"])
    creds = Credentials.from_service_account_info(creds_dict, scopes=scope)
    client = gspread.authorize(creds)
    return client

# ---------------------------------------------------------
# FUNZIONI DI LETTURA CON CACHE OTTIMIZZATA (Evita errore 429)
# ---------------------------------------------------------
@st.cache_data(ttl=300, show_spinner=False)
def carica_utenti_da_sheets():
    try:
        client = init_connection()
        sheet = client.open("VanGo_DB").worksheet("Utenti")
        data = sheet.get_all_records()
        return pd.DataFrame(data)
    except Exception as e:
        st.error(f"Errore caricamento utenti: {e}")
        return pd.DataFrame(columns=["Nome"])

@st.cache_data(ttl=300, show_spinner=False)
def carica_db_da_google_sheets_cached():
    try:
        client = init_connection()
        sheet = client.open("VanGo_DB").worksheet("Clienti")
        data = sheet.get_all_records()
        return pd.DataFrame(data)
    except Exception as e:
        st.error(f"Errore caricamento clienti: {e}")
        return pd.DataFrame()

@st.cache_data(ttl=120, show_spinner=False)
def carica_giro_utente_da_sheets(nome_utente):
    """Carica il giro specifico dell'utente dal foglio Google dedicato."""
    try:
        client = init_connection()
        # Mappa ogni utente alla sua scheda dedicata o legge filtrando
        sheet = client.open("VanGo_DB").worksheet(f"Giro_{nome_utente}")
        data = sheet.get_all_records()
        return pd.DataFrame(data)
    except Exception:
        # Se la scheda non esiste ancora, restituisce un DataFrame vuoto con colonne standard
        return pd.DataFrame(columns=["Cliente", "Indirizzo", "Città", "Note", "Completato"])

def salva_giro_utente_su_sheets(nome_utente, df):
    """Salva il giro specifico dell'utente sul foglio Google dedicato."""
    try:
        client = init_connection()
        doc = client.open("VanGo_DB")
        try:
            sheet = doc.worksheet(f"Giro_{nome_utente}")
        except gspread.exceptions.WorksheetNotFound:
            sheet = doc.add_worksheet(title=f"Giro_{nome_utente}", rows=100, cols=10)
        
        sheet.clear()
        if not df.empty:
            sheet.update([df.columns.values.tolist()] + df.values.tolist())
        st.cache_data.clear()
    except Exception as e:
        st.error(f"Errore nel salvataggio del giro per {nome_utente}: {e}")

# ---------------------------------------------------------
# GESTIONE SESSIONE UTENTE (MULTIUTENTE INDIPENDENTE)
# ---------------------------------------------------------
if "utente_corrente" not in st.session_state:
    st.session_state.utente_corrente = None

# Barra Laterale (Sidebar)
st.sidebar.title("🚐 VanGo - Pannello")

# Caricamento utenti
df_utenti = carica_utenti_da_sheets()
lista_utenti = df_utenti["Nome"].tolist() if not df_utenti.empty and "Nome" in df_utenti.columns else ["Maurizio", "Roberta"]

# Selezione utente indipendente persistente nella sessione
selected_user = st.sidebar.selectbox(
    "Seleziona Utente:", 
    lista_utenti, 
    index=lista_utenti.index(st.session_state.utente_corrente) if st.session_state.utente_corrente in lista_utenti else 0
)

if st.sidebar.button("Conferma / Cambia Utente"):
    st.session_state.utente_corrente = selected_user
    st.success(f"Utente attivo: {selected_user}")
    st.rerun()

st.sidebar.markdown(f"**👤 Utente Corrente:** `{st.session_state.utente_corrente or 'Nessuno'}`")

st.sidebar.markdown("---")

# Pulsante per forzare l'aggiornamento e svuotare la cache
if st.sidebar.button("🔄 Forza Aggiornamento / Svuota Cache"):
    st.cache_data.clear()
    st.sidebar.success("Cache svuotata! Dati ricaricati.")
    st.rerun()

# ---------------------------------------------------------
# CORPO PRINCIPALE DELL'APPLICAZIONE
# ---------------------------------------------------------
st.title("🚐 VanGo - Gestione Trasporti e Consegne")

if not st.session_state.utente_corrente:
    st.warning("⚠️ Seleziona e conferma un utente dalla barra laterale (sidebar) per iniziare a gestire il tuo giro.")
else:
    menu = st.selectbox("Seleziona Sezione", ["🧭 Gestione Giro", "📋 Database Clienti", "⚙️ Impostazioni"])

    if menu == "🧭 Gestione Giro":
        st.header(f"Giro Attivo di {st.session_state.utente_corrente}")
        
        # Carica il giro specifico dell'utente corrente
        df_giri = carica_giro_utente_da_sheets(st.session_state.utente_corrente)
        
        if not df_giri.empty:
            st.dataframe(df_giri, use_container_width=True)
        else:
            st.info(f"Nessun giro memorizzato attualmente per {st.session_state.utente_corrente}.")
            
        col1, col2 = st.columns(2)
        with col1:
            if st.button("🗑️ Svuota Giro Corrente"):
                df_vuoto = pd.DataFrame(columns=["Cliente", "Indirizzo", "Città", "Note", "Completato"])
                salva_giro_utente_su_sheets(st.session_state.utente_corrente, df_vuoto)
                st.warning("Il giro è stato svuotato.")
                st.rerun()

    elif menu == "📋 Database Clienti":
        st.header("Anagrafica Clienti")
        df_clienti = carica_db_da_google_sheets_cached()
        if not df_clienti.empty:
            st.dataframe(df_clienti, use_container_width=True)
        else:
            st.info("Nessun cliente trovato nel database.")

    elif menu == "⚙️ Impostazioni":
        st.header("Impostazioni dell'App")
        st.write("Configurazioni di sistema e sincronizzazione Google Sheets.")
        st.text(f"Consumer Project: 789116577376")
        st.text(f"Utente attualmente loggato: {st.session_state.utente_corrente}")
