# Profilul comun AyuGram

Managerul pregătește acest profil doar în copia separată `.ayugram`, după
verificarea versiunii clientului și înainte de lansare. O versiune incompatibilă
nu primește profilul și nu creează o copie nouă.

Butonul „Autentificare nouă în AyuGram” oferă alternativa fără import: creează
un profil gol în `.ayugram`, aplică preferințele și deschide autentificarea prin
QR sau număr de telefon. Sesiunea Telegram originală nu este copiată. O copie
AyuGram importată deja existentă nu este resetată; un profil creat pentru
autentificare nouă este reutilizat, inclusiv după autentificare, prin
„Open with AyuGram”. Verificarea versiunii rămâne activă pentru cheile existente.

La prima autentificare nouă, dacă profilul nu are încă setările native AyuGram,
Managerul pregătește profilul gol în fundal, cere Quit normal și verifică
închiderea înainte de a aplica preferințele comune și a deschide autentificarea.
Această pregătire evită ignorarea preferințelor de către primul startup
AyuGram 7.2.9. Nu importă sesiunea Telegram și nu resetează profilurile existente.

Telegram și profilul separat AyuGram pot rula simultan. Cardul indică starea
fiecărui client și oferă „Close Telegram” și „Close AyuGram” separat. „Close All”
închide ambele aplicații. Importul inițial al sesiunii Telegram cere ca Telegram
să fie închis; autentificarea nouă AyuGram nu cere închiderea lui. Operațiile de
restaurare, mutare și curățare păstrează verificarea pentru oricare client activ.

Close cere mai întâi Quit prin macOS pentru PID-ul și executabilul exacte.
Dacă procesul nu se închide în 8 secunde, se trimite SIGTERM, apoi SIGKILL numai
dacă rămâne activ încă 4 secunde. Identitatea procesului este verificată înainte
de fiecare semnal; o verificare indisponibilă oprește escaladarea.

La deschidere, clona AyuGram primește denumirea custom din Manager (sau numele
folderului dacă nu există una). Pentru masterul oficial se modifică doar `CFBundleName` și
`CFBundleDisplayName` din `Info.plist`, fără modificarea executabilului și fără
resemnare. Verificarea clonei permite diferențe doar în aceste două câmpuri;
toate celelalte fișiere și câmpuri rămân verificate față de masterul oficial.
O denumire schimbată în Manager se aplică la următoarea deschidere AyuGram.
Titlul ferestrei este controlat de AyuGram și nu preia această denumire; la
autentificare rămâne „AyuGram”.

Managerul acceptă și buildul custom cu mesajele șterse restaurate în
conversații după restart. Pentru
acest build semnat se redenumește doar bundle-ul clonei: executabilul și
Info.plist rămân identice cu masterul, iar semnătura este păstrată. Profilul
fiecărui cont rămâne în `.ayugram/TelegramForcePortable`; Managerul transmite
explicit acest workdir și validează izolarea înainte de lansare. Transparența
este activată o singură dată, păstrând apoi alegerile utilizatorului.

Verificarea TDF rămâne activă: baza AyuGram 7.2.9 nu poate importa o sesiune
Telegram 7.3.1. Panoul de actualizări identifică buildul custom și avertizează
înainte ca o versiune oficială să îl înlocuiască și să elimine restaurarea în chat.

## Avertizarea înainte de actualizarea Telegram

„Update Telegram” verifică semnătura și versiunea aplicației alese, apoi compară
versiunea cu masterul AyuGram. Dacă Telegram este mai nou sau versiunea AyuGram
nu poate fi citită, apare o avertizare înainte de instalare, cu „Renunță” și
„Continuă actualizarea Telegram”. Backendul reface verificarea și cere
confirmarea perechii actuale de versiuni. Nu sunt editate sesiuni în această
verificare, iar instalarea unei versiuni Telegram mai vechi decât masterul este
refuzată. O comparație favorabilă de versiuni nu certifică compatibilitatea
formatului și nu înlocuiește testul pe copie.

Managerul dezactivează updaterul intern Telegram prin mecanismul oficial
`externalupdater.d/telegram-manager`, păstrat în folderul contului, în afara
`TelegramForcePortable/tdata`. Fișierul conține calea bundle-ului Telegram, nu
date de autentificare. Este creat pentru clonele noi și actualizat înainte de
fiecare lansare, inclusiv după schimbarea denumirii. La lansare se transmite și
`-noupdate`, pentru a nu aplica o actualizare pregătită anterior. Nu sunt
modificate executabilul, fișierele sesiunii sau preferințele criptate.

Instanțele pornite înainte de aplicarea protecției o preiau la următoarea
deschidere. Actualizările inițiate din Manager trec prin avertizare; instalările
manuale din afara Managerului rămân în afara acestui control.

Copiile AyuGram folosesc aceeași protecție oficială, în
`.ayugram/externalupdater.d/telegram-manager`, și sunt pornite cu `-noupdate`.
Opțiunea `-ghost` este păstrată când preferințele cer Ghost Mode. Controlul
updaterului nu este scris în `tdata` și nu schimbă executabilul sau numele
aplicației. Este recreat/verificat la fiecare lansare din Manager, inclusiv
după înlocuirea controlată a masterului AyuGram. Instanțele deja pornite preiau
protecția la redeschidere; pachetul oficial nou trebuie instalat explicit,
păstrând verificarea provenienței și a versiunii sesiunii.

- Ghost Mode global: fără confirmări de citire a mesajelor sau poveștilor,
  fără trimiterea stării online și a progresului încărcărilor; trimiterea stării
  offline după online este activată. Marcarea automată ca citit după o acțiune
  este dezactivată.
- Traducător: furnizorul Telegram (`translationProvider: "telegram"`).
- ID-uri: afișate în format Bot API (`showPeerId: 2`).
- Save Deleted Messages și Save Messages History: activate. Mesajele primite
  și istoricul lor pot fi păstrate local în copia AyuGram; acestea ocupă spațiu
  și nu garantează recuperarea mesajelor șterse înainte de a fi primite.

Doar aceste preferințe sunt combinate cu fișierul
`.ayugram/TelegramForcePortable/tdata/ayu_settings.json`. Celelalte preferințe
sunt păstrate. Fișierele de autentificare și setările criptate nu sunt editate.
Fișierul original `TelegramForcePortable/tdata` nu este modificat.

Profilul se aplică o singură dată pentru fiecare copie și versiune a profilului;
marcajul `.ayugram/.common-profile.json` împiedică suprascrierea modificărilor
făcute ulterior de utilizator în AyuGram. Dacă AyuGram elimină complet intrarea
Ghost Mode globală, Managerul o repară numai când modul global este încă activ;
preferințele explicite existente sunt păstrate. Un profil actualizat se aplică la
următoarea deschidere din Manager, când copia este închisă. Profilul este definit
în `TelegramManager.app/Contents/Resources/ayugram.py`, `COMMON_PROFILE`.

## Ce rămâne de verificat

Butonul de traducere este o preferință internă criptată Telegram, separată de
furnizorul AyuGram. Nu este activat prin acest profil. Activarea lui și folosirea
funcțiilor profilului trebuie verificate în aplicație pe o copie izolată când există
un build oficial compatibil. Nu se forțează traducerea automată a conversațiilor.

Formatul preferințelor a fost verificat în sursele oficiale AyuGram 7.2.9:
https://github.com/AyuGram/AyuGramDesktop/blob/v7.2.9/Telegram/SourceFiles/ayu/ayu_settings.cpp
https://github.com/AyuGram/AyuGramDesktop/blob/v7.2.9/Telegram/SourceFiles/ayu/ayu_settings.h
https://github.com/AyuGram/AyuGramDesktop/blob/v7.2.9/Telegram/SourceFiles/boxes/translate_box.cpp

La prima versiune compatibilă se verifică din nou formatul preferințelor și
comportamentul real al Ghost Mode. Ghost Mode nu garantează anonimatul față de
Telegram sau lipsa oricărei activități vizibile.
