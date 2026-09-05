import streamlit as st
import pandas as pd
import urllib.parse
from streamlit_sortables import sort_items

# Configurazione della pagina
st.set_page_config(
    page_title="VanGo - Gestione Consegne",
    page_icon="🚐",
    layout="centered"
)

# ==========================================
# STILE CSS PERSONALIZZATO
# ==========================================
st.markdown("""
<style>
    .stop-card {
        background-color: #1E1E1E;
        border: 1px solid #333333;
        padding: 15px;
        border-radius: 12px;
        margin-bottom: 12px;
    }
    .stop-title {
        font-size: 16px;
        font-weight: bold;
        color: #FFFFFF;
        margin-bottom: 5px;
    }
    .stop-address {
        font-size: 14px;
        color: #94A3B8;
        margin-bottom: 8px;
    }
    .stop-meta {
        font-size: 13px;
        color: #38BDF8;
    }
    .clean-card {
        background-color: #1E293B;
        border: 1px solid #334155;
        padding: 14px;
        border-radius: 10px;
        margin-bottom: 10px;
        display: flex;
        align-items: center;
    }
    .clean-badge {
        background-color: #2563EB;
        color: white;
        font-weight: bold;
        width: 30px;
        height: 30px;
        border-radius: 50%;
        display: flex;
        align-items: center;
        justify-content: center;
        margin-right: 12px;
        flex-shrink: 0;
    }
    .clean-content {
        flex-grow: 1;
    }
    .clean-title {
        font-size: 15px;
        font-weight: bold;
        color: #F8FAFC;
    }
    .clean-subtitle {
        font-size: 13px;
        color: #94A3B8;
    }
</style>
""", unsafe_allow_html=True)

# ==========================================
# 1. INIZIALIZZAZIONE STATO DI SESSIONE
# ==========================================
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
if "username" not in st.session_state:
    st.session_state.username = ""
if "ruolo" not in st.session_state:
    st.session_state.ruolo = ""  # "admin" oppure "autista"
if "giro_corrente" not in st.session_state:
    st.session_state.giro_corrente = pd.DataFrame(columns=['CLIENTE', 'VIA', 'COMUNE', 'ORA', 'Q.ta'])
if "pagina_corrente" not in st.session_state:
    st.session_state.pagina_corrente = "home"
if "vista_pulita" not in st.session_state:
    st.session_state.vista_pulita = False

# ==========================================
# 2. FUNZIONI DI SUPPORTO E GITHUB
# ==========================================
def scarica_db_da_github():
    url_github = "https://raw.githubusercontent.com/tuo-utente/tuo-repo/main/database_clienti.csv"
    try:
        df = pd.read_csv(url_github)
        return df
    except Exception as e:
        st.warning("⚠️ Impossibile connettersi a GitHub in questo momento. Assicurati che l'URL raw sia corretto.")
        return pd.DataFrame(columns=['CLIENTE', 'VIA', 'COMUNE', 'ORA', 'Q.ta'])

def salva_giro_su_disco(df):
    pass

# ==========================================
# 3. SCHERMATA DI LOGIN
# ==========================================
if not st.session_state.logged_in:
    st.markdown("<h2 style='text-align: center;'>🚐 VanGo - Accesso</h2>", unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: #94A3B8;'>Inserisci le credenziali per accedere al terminale di bordo.</p>", unsafe_allow_html=True)
    
    col_l1, col_l2, col_l3 = st.columns([1, 2, 1])
    with col_l2:
        with st.form("form_login"):
            input_user = st.text_input("Username")
            input_pass = st.text_input("Password", type="password")
            
            st.markdown("<div style='margin-top: 10px;'></div>", unsafe_allow_html=True)
            submit_login = st.form_submit_button("ACCEDI AL SISTEMA", use_container_width=True)
            
            if submit_login:
                if input_user == "admin" and input_pass == "admin123":
                    st.session_state.logged_in = True
                    st.session_state.username = input_user
                    st.session_state.ruolo = "admin"
                    st.session_state.pagina_corrente = "home"
                    st.rerun()
                elif input_user == "autista" and input_pass == "autista123":
                    st.session_state.logged_in = True
                    st.session_state.username = input_user
                    st.session_state.ruolo = "autista"
                    st.session_state.pagina_corrente = "home"
                    
                    with st.spinner("Sincronizzazione database da GitHub in corso..."):
                        st.session_state.giro_corrente = scarica_db_da_github()
                    st.rerun()
                else:
                    st.error("❌ Credenziali errate. Riprova.")
    
    st.stop()

# ==========================================
# 4. BARRA LATERALE (INFO UTENTE & LOGOUT)
# ==========================================
with st.sidebar:
    st.markdown(f"👤 Utente: **{st.session_state.username}**")
    st.markdown(f"🛡️ Profilo: **{st.session_state.ruolo.upper()}**")
    st.markdown("---")
    
    if st.button("🏠 Home Grafica", use_container_width=True):
        st.session_state.pagina_corrente = "home"
        st.rerun()

    if st.button("🚪 Esci / Logout", use_container_width=True):
        st.session_state.logged_in = False
        st.session_state.username = ""
        st.session_state.ruolo = ""
        st.session_state.giro_corrente = pd.DataFrame(columns=['CLIENTE', 'VIA', 'COMUNE', 'ORA', 'Q.ta'])
        st.session_state.pagina_corrente = "home"
        st.rerun()

# ==========================================
# 5. GESTIONE NAVIGAZIONE TRA LE SCHERMATE
# ==========================================
tot_clienti = len(st.session_state.giro_corrente)
tot_qta = int(st.session_state.giro_corrente['Q.ta'].sum()) if 'Q.ta' in st.session_state.giro_corrente.columns and tot_clienti > 0 else 0
tot_comuni = int(st.session_state.giro_corrente['COMUNE'].nunique()) if 'COMUNE' in st.session_state.giro_corrente.columns and tot_clienti > 0 else 0

# --- SCHERMATA HOME GRAFICA ---
if st.session_state.pagina_corrente == "home":
    if st.sidebar.button("🔙 Torna alla Home", use_container_width=True) if "home" != st.session_state.pagina_corrente else None:
        pass
        
    st.markdown("<h1 style='text-align: center;'>🚐 VANGO</h1>", unsafe_allow_html=True)
    st.markdown("<div style='margin-bottom: 20px;'></div>", unsafe_allow_html=True)

    if st.button("📍 GIRO DEL GIORNO", use_container_width=True):
        st.session_state.pagina_corrente = "giro"
        st.rerun()

    if st.button("📁 INSERISCI CLIENTE", use_container_width=True):
        st.session_state.pagina_corrente = "inserisci"
        st.rerun()

    if st.button("🔄 INVERTI SEQUENZA", use_container_width=True):
        if not st.session_state.giro_corrente.empty:
            st.session_state.giro_corrente = st.session_state.giro_corrente.iloc[::-1].reset_index(drop=True)
            salva_giro_su_disco(st.session_state.giro_corrente)
            st.success("Sequenza invertita con successo!")
            st.rerun()
        else:
            st.warning("Nessun giro da invertire.")

    if st.button("🗑️ SVUOTA GIRO", use_container_width=True):
        st.session_state.giro_corrente = pd.DataFrame(columns=['CLIENTE', 'VIA', 'COMUNE', 'ORA', 'Q.ta'])
        salva_giro_su_disco(st.session_state.giro_corrente)
        st.success("Giro svuotato.")
        st.rerun()

    st.markdown("<div style='margin-top: 30px;'></div>", unsafe_allow_html=True)
    
    # Metriche riassuntive stile dashboard
    st.markdown("### Fermate Totali")
    st.markdown(f"<h2 style='color: #38BDF8; margin-top: -10px;'>{tot_clienti}</h2>", unsafe_allow_html=True)
    
    st.markdown("### Pezzi Totali")
    st.markdown(f"<h2 style='color: #38BDF8; margin-top: -10px;'>{tot_qta}</h2>", unsafe_allow_html=True)
    
    st.markdown("### Comuni")
    st.markdown(f"<h2 style='color: #38BDF8; margin-top: -10px;'>{tot_comuni}</h2>", unsafe_allow_html=True)

# --- SCHERMATA INSERISCI CLIENTE / DATABASE ---
elif st.session_state.pagina_corrente == "inserisci":
    if st.button("⬅️ Indietro alla Home"):
        st.session_state.pagina_corrente = "home"
        st.rerun()
        
    st.subheader("📁 Gestione Database")

    if st.session_state.ruolo == "admin":
        st.success("Pannello Admin attivo: puoi caricare file Excel/CSV locali.")
        uploaded_file = st.file_uploader("Carica nuovo file Excel/CSV", type=["xlsx", "csv"])
        if uploaded_file is not None:
            if uploaded_file.name.endswith('.csv'):
                st.session_state.giro_corrente = pd.read_csv(uploaded_file)
            else:
                st.session_state.giro_corrente = pd.read_excel(uploaded_file)
            st.success("Database caricato con successo!")
            st.rerun()

    elif st.session_state.ruolo == "autista":
        st.info("ℹ️ Profilo Autista: Database sincronizzato automaticamente in sola lettura da GitHub.")
        if st.button("🔄 Aggiorna dati da GitHub", use_container_width=True):
            with st.spinner("Scaricamento aggiornamenti in corso..."):
                st.session_state.giro_corrente = scarica_db_da_github()
            st.success("Database aggiornato correttamente!")
            st.rerun()

# --- SCHERMATA GIRO DEL GIORNO ---
elif st.session_state.pagina_corrente == "giro":
    if st.button("⬅️ Indietro alla Home"):
        st.session_state.pagina_corrente = "home"
        st.rerun()

    st.subheader("🚐 Giro Consegne Attivo")

    if tot_clienti > 0:
        col_m1, col_m2, col_m3 = st.columns(3)
        col_m1.metric("Fermate", f"{tot_clienti}")
        col_m2.metric("Pezzi", f"{tot_qta}")
        col_m3.metric("Comuni", f"{tot_comuni}")

        st.markdown("---")

        label_btn_vista = "👁️ TORNA ALLA VISTA OPERATIVA" if st.session_state.vista_pulita else "📋 VISTA RIEPILOGO PULITA"
        if st.button(label_btn_vista, use_container_width=True):
            st.session_state.vista_pulita = not st.session_state.vista_pulita
            st.rerun()
        st.markdown("<div style='margin-bottom: 10px;'></div>", unsafe_allow_html=True)

        addresses = [f"{r.get('VIA', '')}, {r.get('COMUNE', '')}" for _, r in st.session_state.giro_corrente.iterrows()]
        if len(addresses) == 1:
            maps_url = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(addresses[0])}"
        else:
            origin = urllib.parse.quote(addresses[0])
            destination = urllib.parse.quote(addresses[-1])
            waypoints = "|".join([urllib.parse.quote(a) for a in addresses[1:-1]])
            maps_url = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={destination}&waypoints={waypoints}"

        if st.session_state.vista_pulita:
            for idx in range(tot_clienti):
                row = st.session_state.giro_corrente.iloc[idx]
                st.markdown(f"""
                <div class="clean-card">
                    <div class="clean-badge">{idx + 1}</div>
                    <div class="clean-content">
                        <div class="clean-title">{row.get('VIA', '')}</div>
                        <div class="clean-subtitle">{row.get('COMUNE', '')} — Cliente: {row.get('CLIENTE', '')} (🕒 {row.get('ORA', '')} | 📦 {row.get('Q.ta', 0)} pz)</div>
                    </div>
                </div>
                """, unsafe_allow_html=True)
        else:
            st.markdown("<p style='color: #94A3B8; font-size: 13px;'>💡 Trascina le schede per riordinare le fermate:</p>", unsafe_allow_html=True)
            
            items_data = []
            for idx, row in st.session_state.giro_corrente.iterrows():
                items_data.append({
                    "header": f"{idx + 1}. {row.get('CLIENTE', 'Cliente')}",
                    "content": f"📍 {row.get('VIA', '')}, {row.get('COMUNE', '')} | 🕒 {row.get('ORA', '')} | 📦 {row.get('Q.ta', 0)} pz"
                })

            sorted_items = sort_items([{"items": items_data}], key="giro_sortable")

            if sorted_items and "items" in sorted_items[0]:
                new_headers = [item["header"] for item in sorted_items[0]["items"]]
                new_clienti_order = [h.split(". ", 1)[1] for h in new_headers]
                
                current_clienti_order = st.session_state.giro_corrente['CLIENTE'].tolist()
                if new_clienti_order != current_clienti_order:
                    st.session_state.giro_corrente = (
                        st.session_state.giro_corrente.set_index('CLIENTE')
                        .loc[new_clienti_order]
                        .reset_index()
                    )
                    salva_giro_su_disco(st.session_state.giro_corrente)
                    st.rerun()

            st.markdown("---")
            
            for idx in range(tot_clienti):
                row = st.session_state.giro_corrente.iloc[idx]
                
                st.markdown(f"""
                <div class="stop-card">
                    <div class="stop-title">{idx + 1}. {row.get('CLIENTE', '')}</div>
                    <div class="stop-address">📍 {row.get('VIA', '')}, {row.get('COMUNE', '')}</div>
                    <div class="stop-meta">🕒 Ora: {row.get('ORA', '')} | 📦 Q.tà: {row.get('Q.ta', 0)} pz</div>
                </div>
                """, unsafe_allow_html=True)

                col_c1, col_c2 = st.columns([1, 1])
                with col_c1:
                    dest = urllib.parse.quote(f"{row.get('VIA', '')}, {row.get('COMUNE', '')}")
                    st.write("")
                    st.markdown(f"[🚘 **NAVIGA ORA**](https://www.google.com/maps/dir/?api=1&destination={dest})")

                with col_c2:
                    nuova_qta = st.number_input(
                        "Q.tà colli",
                        min_value=0,
                        value=int(row.get('Q.ta', 0)),
                        key=f"qta_mobile_{row.get('CLIENTE', '')}_{idx}"
                    )
                    if nuova_qta != int(row.get('Q.ta', 0)):
                        st.session_state.giro_corrente.at[idx, 'Q.ta'] = nuova_qta
                        salva_giro_su_disco(st.session_state.giro_corrente)
                        st.rerun()

                st.markdown("<hr style='margin: 10px 0; border-color: #262626;'>", unsafe_allow_html=True)

        st.markdown("---")
        st.markdown(f'''
            <a href="{maps_url}" target="_blank" style="text-decoration:none;">
                <button style="width:100%; background-color:#2563EB; color:white; border:none; border-radius:25px; height:52px; font-weight:bold; font-size:16px; box-shadow: 0 4px 10px rgba(37, 99, 235, 0.4);">
                    🗺️ AVVIA PERCORSO COMPLETO
                </button>
            </a>
        ''', unsafe_allow_html=True)
    else:
        st.info("Nessuna fermata disponibile nel giro. Sincronizza il database da GitHub o carica i dati.")
