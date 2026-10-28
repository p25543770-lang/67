; Depth Studio: изолированная среда Python создаётся при первом запуске в
; %LOCALAPPDATA%\DepthStudio. Удаляем её при удалении приложения, чтобы не
; оставлять сотни мегабайт в профиле пользователя.
!macro customUnInstall
  DetailPrint "Удаление среды Depth Studio..."
  RMDir /r "$LOCALAPPDATA\DepthStudio\runtime"
  RMDir /r "$LOCALAPPDATA\DepthStudio\state"
  RMDir /r "$LOCALAPPDATA\DepthStudio\cache"
  RMDir /r "$LOCALAPPDATA\DepthStudio\logs"
  RMDir "$LOCALAPPDATA\DepthStudio"
!macroend
