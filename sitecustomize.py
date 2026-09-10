"""VanGo TEST only: redirect the existing gspread database title to the TEST spreadsheet.

Production appInfo.py remains untouched. Python loads sitecustomize before Streamlit imports
appInfo.py, so the existing call client.open("VanGo Database") is transparently redirected.
"""

import gspread

TEST_SPREADSHEET_ID = "1OMdvDb7Ttgj6lZxr3kinLqv3Gry130RSXo7xnZaLyRE"
_original_open = gspread.Client.open


def _open_test_database(self, title, *args, **kwargs):
    if str(title).strip() == "VanGo Database":
        return self.open_by_key(TEST_SPREADSHEET_ID)
    return _original_open(self, title, *args, **kwargs)


gspread.Client.open = _open_test_database
