# Guida alla Distribuzione di CLEM

## 1. Installazione Locale
1. Assicurati di avere Python 3.9+ installato.
2. Clona la cartella del progetto.
3. Installa le dipendenze:
   ```bash
   pip install -r requirements.txt
   ```
4. Configura l'ambiente:
   - Copia `.env.example` in `.env`.
   - Inserisci la tua `ANTHROPIC_API_KEY`.
5. Avvia l'app:
   ```bash
   streamlit run app.py
   ```

## 2. Distribuzione Web (Streamlit Cloud)
1. Carica il progetto su un repository GitHub.
2. Vai su [share.streamlit.io](https://share.streamlit.io).
3. Collega il repository e seleziona `app.py`.
4. Nelle impostazioni (Settings -> Secrets), aggiungi la tua chiave:
   ```toml
   ANTHROPIC_API_KEY = "la-tua-chiave-qui"
   ```

## 3. Note sull'Efficienza
- L'app utilizza `@st.cache_data` per minimizzare le chiamate API ad Anthropic.
- Gli indici PUN/PSV hanno una cache di 1 ora in sessione e 24 ore su disco.
- Le offerte ARERA hanno una cache di 15 giorni per ridurre i consumi di token.
