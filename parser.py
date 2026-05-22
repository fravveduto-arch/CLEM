"""
CLEM - Claude Energy Monitor
parser.py — Parser multi-fornitore per bollette energetiche italiane.

Fornitori supportati (quota mercato centro-sud):
  Edison, Enel Energia, Eni Plenitude, A2A Energia,
  Illumia, Hera Comm, Iren Energia, Acea Energia, Green Network
"""

import pdfplumber
import re
import io
from typing import Union


class EnergyParser:
    """
    Accetta sia un path su disco che un oggetto BytesIO (upload in memoria).
    Restituisce un dict con chiavi standardizzate:
      vendor, type, bill_date, consumption, spread, total_cost,
      fixed_cost, price_per_unit, pod, pdr
    """

    VENDOR_SIGNATURES = {
        'Edison':         ['Edison Energia', 'EDISON ENERGIA', 'edison.it'],
        'Enel':           ['Enel Energia', 'ENEL ENERGIA', 'enel.it'],
        'Eni Plenitude':  ['Eni Plenitude', 'ENI PLENITUDE', 'plenitude.it', 'Eni gas e luce'],
        'A2A':            ['A2A Energia', 'A2A ENERGIA', 'a2aenergia.it'],
        'Illumia':        ['Illumia', 'ILLUMIA', 'illumia.com'],
        'Hera':           ['Hera Comm', 'HERAcomm', 'HERA COMM', 'heracomm.it'],
        'Iren':           ['Iren Energia', 'IREN ENERGIA', 'irenenergia.it'],
        'Acea':           ['Acea Energia', 'ACEA ENERGIA', 'aceaenergia.it'],
        'Green Network':  ['Green Network', 'GREEN NETWORK', 'greennetwork.it'],
    }

    def __init__(self, source: Union[str, io.BytesIO]):
        self.source = source
        self.text = ""
        self.data = {}
        self._load_pdf()

    def _load_pdf(self):
        try:
            with pdfplumber.open(self.source) as pdf:
                for i in range(min(3, len(pdf.pages))):
                    page_text = pdf.pages[i].extract_text()
                    if page_text:
                        self.text += page_text + "\n"
        except Exception as e:
            print(f"[CLEM Parser] Errore lettura PDF: {e}")

    def parse(self) -> dict:
        self.data = {}
        vendor = self._detect_vendor()
        self.data['vendor'] = vendor

        bill_type = self._detect_type()
        self.data['type'] = bill_type

        self._parse_date()
        self._parse_billing_period()
        self._parse_consumption(bill_type)
        self._parse_costs(vendor, bill_type)
        self._parse_identifiers(bill_type)
        self._parse_index_details(bill_type)

        return self.data

    # ── Detection ────────────────────────────────────────────────────────────

    def _detect_vendor(self) -> str:
        for vendor, signatures in self.VENDOR_SIGNATURES.items():
            for sig in signatures:
                if sig in self.text:
                    return vendor
        return 'Sconosciuto'

    def _detect_type(self) -> str:
        gas_keywords = [
            'GAS NATURALE', 'gas naturale', 'fornitura gas',
            'FORNITURA GAS', 'PDR', 'Smc', 'SMC',
            'spesa per il gas', 'SPESA PER IL GAS'
        ]
        for kw in gas_keywords:
            if kw in self.text:
                return 'Gas'
        return 'Luce'

    # ── Field Parsers ─────────────────────────────────────────────────────────

    def _parse_date(self):
        patterns = [
            r'del\s+(\d{2}/\d{2}/\d{4})',
            r'Data\s+emissione[:\s]+(\d{2}/\d{2}/\d{4})',
            r'Periodo[:\s]+\S+\s+(\d{2}/\d{2}/\d{4})',
            r'(\d{2}/\d{2}/\d{4})',  # fallback: prima data trovata
        ]
        for p in patterns:
            m = re.search(p, self.text)
            if m:
                self.data['bill_date'] = m.group(1)
                return
        self.data['bill_date'] = ''

    def _parse_billing_period(self):
        """
        Rileva il periodo di fatturazione (data inizio e fine) e calcola
        la durata in giorni. Imposta billing_days e period_type
        ('mensile', 'bimestrale', 'stimato').
        """
        # Pattern: "dal DD/MM/YYYY al DD/MM/YYYY" oppure "dal DD/MM/YYYY al DD/MM/YYYY"
        period_patterns = [
            r'dal\s+(\d{2}/\d{2}/\d{4})\s+al\s+(\d{2}/\d{2}/\d{4})',
            r'periodo\s+(?:di\s+fornitura\s+)?(?:dal\s+)?(\d{2}/\d{2}/\d{4})\s*[-–]\s*(\d{2}/\d{2}/\d{4})',
            r'(\d{2}/\d{2}/\d{4})\s*[-–/]\s*(\d{2}/\d{2}/\d{4})',
        ]

        import datetime
        for p in period_patterns:
            m = re.search(p, self.text, re.IGNORECASE)
            if m:
                try:
                    d1 = datetime.datetime.strptime(m.group(1), '%d/%m/%Y').date()
                    d2 = datetime.datetime.strptime(m.group(2), '%d/%m/%Y').date()
                    days = abs((d2 - d1).days)
                    if 10 < days < 400:  # sanity check
                        self.data['billing_days'] = days
                        if days <= 45:
                            self.data['period_type'] = 'mensile'
                        elif days <= 75:
                            self.data['period_type'] = 'bimestrale'
                        elif days <= 105:
                            self.data['period_type'] = 'trimestrale'
                        else:
                            self.data['period_type'] = 'stimato'
                        return
                except ValueError:
                    continue

        # Fallback: euristica su consumo (impostato dopo, quindi usiamo keyword)
        # Se troviamo "mensile" o "bimestrale" esplicitamente nel testo
        if re.search(r'\bmensil\w+\b', self.text, re.IGNORECASE):
            self.data['billing_days'] = 30
            self.data['period_type'] = 'mensile'
        elif re.search(r'\bbimestral\w+\b', self.text, re.IGNORECASE):
            self.data['billing_days'] = 60
            self.data['period_type'] = 'bimestrale'
        else:
            self.data['billing_days'] = 0
            self.data['period_type'] = 'stimato'

    def _parse_consumption(self, bill_type: str):
        if bill_type == 'Gas':
            patterns = [
                r'Consumo\s+(?:del\s+periodo\s+)?([\d,.]+)\s*[Ss]mc',
                r'Volume\s+(?:fatturato\s+)?([\d,.]+)\s*[Ss]mc',
                r'([\d,.]+)\s*[Ss]mc\b',
            ]
        else:
            patterns = [
                r'Consumo\s+(?:del\s+periodo\s+)?([\d,.]+)\s*kWh',
                r'Quota\s+consumi\s+([\d,.]+)\s*kWh',
                r'Energia\s+(?:attiva\s+)?([\d,.]+)\s*kWh',
                r'([\d,.]+)\s*kWh\b',
            ]
        for p in patterns:
            m = re.search(p, self.text)
            if m:
                try:
                    self.data['consumption'] = float(m.group(1).replace('.', '').replace(',', '.'))
                    return
                except ValueError:
                    continue
        self.data['consumption'] = 0.0

    def _parse_costs(self, vendor: str, bill_type: str):
        # Spread (offerte variabili indicizzate)
        if bill_type == 'Gas':
            spread_patterns = [
                r'A\s*[=:]\s*([\d,]+)\s*€/[Ss]mc',
                r'spread\s*[=:]\s*([\d,]+)\s*€/[Ss]mc',
                r'quota\s+energia\s+([\d,]+)\s*€/[Ss]mc',
            ]
        else:
            spread_patterns = [
                r'A\s*[=:]\s*([\d,]+)\s*€/kWh',
                r'spread\s*[=:]\s*([\d,]+)\s*€/kWh',
                r'quota\s+energia\s+([\d,]+)\s*€/kWh',
            ]
        self.data['spread'] = 0.0
        for p in spread_patterns:
            m = re.search(p, self.text, re.IGNORECASE)
            if m:
                try:
                    self.data['spread'] = float(m.group(1).replace(',', '.'))
                    break
                except ValueError:
                    continue

        # Prezzo unitario fisso (offerte a prezzo bloccato)
        if bill_type == 'Gas':
            price_patterns = [r'([\d,]+)\s*€/[Ss]mc']
        else:
            price_patterns = [r'([\d,]+)\s*€/kWh']
        self.data['price_per_unit'] = 0.0
        for p in price_patterns:
            m = re.search(p, self.text)
            if m:
                try:
                    val = float(m.group(1).replace(',', '.'))
                    # Filtriamo valori impossibili
                    if bill_type == 'Gas' and 0.2 < val < 2.0:
                        self.data['price_per_unit'] = val
                        break
                    elif bill_type == 'Luce' and 0.05 < val < 0.8:
                        self.data['price_per_unit'] = val
                        break
                except ValueError:
                    continue

        # Totale bolletta
        total_patterns = [
            r'[Tt]otale\s+(?:da\s+pagare\s+)?[€EUR]?\s*([\d,.]+)',
            r'[Ii]mporto\s+(?:totale\s+)?[€EUR]?\s*([\d,.]+)',
            r'€\s*([\d,.]+)\s*$',
        ]
        self.data['total_cost'] = 0.0
        for p in total_patterns:
            m = re.search(p, self.text, re.MULTILINE)
            if m:
                try:
                    val = float(m.group(1).replace('.', '').replace(',', '.'))
                    if 5.0 < val < 5000.0:  # sanity check
                        self.data['total_cost'] = val
                        break
                except ValueError:
                    continue

    def _parse_identifiers(self, bill_type: str):
        pod_m = re.search(r'POD\s+([A-Z0-9]+)', self.text)
        self.data['pod'] = pod_m.group(1) if pod_m else ''
        pdr_m = re.search(r'PDR\s+(\d+)', self.text)
        self.data['pdr'] = pdr_m.group(1) if pdr_m else ''

    def _parse_index_details(self, bill_type: str):
        """
        Rileva come viene applicato il PUN/PSV e la frequenza di aggiornamento.
        """
        if bill_type == 'Luce':
            # Tipo applicazione PUN
            if re.search(r'monoraria|F0', self.text, re.IGNORECASE):
                self.data['index_mode'] = 'PUN Monorario'
            elif re.search(r'fasce|F1|F2|F3', self.text, re.IGNORECASE):
                self.data['index_mode'] = 'PUN Multi-fascia'
            else:
                self.data['index_mode'] = 'PUN Medio mensile'
            
            self.data['update_freq'] = 'Mensile'
        else:
            # Gas
            self.data['index_mode'] = 'PSV (Media Day-Ahead)'
            self.data['update_freq'] = 'Mensile'

        # Sovrascrittura se troviamo "trimestrale" in relazione ai prezzi
        if re.search(r'aggiornamento\s+trimestrale', self.text, re.IGNORECASE):
            self.data['update_freq'] = 'Trimestrale'
