"""Адаптеры сайта gub.uy: вход, источники календаря, разведка. Только чтение (SPEC 0.3)."""

# Не больше стольких файлов в каталогах `data/quarantine/` и `data/shots/` (SPEC 2.7; те же 20
# в `FILE_RETENTION` очистки, application/housekeeping.py): старейшие удаляются при записи
MAX_STORED_FILES = 20
