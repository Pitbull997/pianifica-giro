"""Launcher TEST di VanGo.

Usato esclusivamente dal branch vango-test. Reindirizza il database chiamato
"VanGo Database" allo Spreadsheet TEST, lasciando invariato appInfo.py.
"""

import gspread

ID_DATABASE_TEST = "1OMdvDb7Ttgj6lZxr3kinLqv3Gry130RSXo7xnZaLyRE"

_apertura_originale = gspread.Client.open


def _apri_database_test(self, titolo, *args, **kwargs):
    if str(titolo).strip() == "VanGo Database":
        return self.open_by_key(ID_DATABASE_TEST)
    return _apertura_originale(self, titolo, *args, **kwargs)


gspread.Client.open = _apri_database_test

# Avvia l'app originale dopo aver applicato il solo redirect TEST.
import appInfo  # noqa: E402,F401
