@echo off
REM Alias so `notionchat serve` works the same as `notion serve` via PATH.
call "%~dp0notion.cmd" %*
