# Milesight „udpp“ – protokol hledání a změny IP (Smart Tools)

Technická poznámka k `netpanel/backend/cameras.py`. Protokol není veřejně dokumentovaný; níže je to, co jsem
zjistil **statickou analýzou** instalátoru *Milesight_Smart Tools_2.4.0.5-r30.exe* (NSIS → `Smart Tools.exe`,
32bit x86, Qt5 + MFC, XML knihovna CMarkup). Cílem byla interoperabilita: aby Raspberry Pi umělo s kamerami
mluvit bez Windows. **Na reálné kameře zatím neověřeno**. Adresy funkcí jsou z `Smart Tools.exe` (image base
`0x400000`), aby šlo cokoli z toho znovu dohledat.

## Transport

| | |
|---|---|
| Cíl | UDP multicast `239.255.255.250`, port **26360** (`0x66F8`, 12 volání `htons(0x66f8)`) |
| Zdroj | socket svázaný s IP zvoleného adaptéru, port 26360 |
| Odpovědi | přijímací socket na portu 26360 + `IP_ADD_MEMBERSHIP 239.255.255.250` (`FUN_007a1fd0`) |
| Opakování | Smart Tools posílá dotaz hned a pak každých ~30 s (smyčka 100 ms, `counter % 300 == 0`) |
| Alternativa | raw Ethernet přes WinPcap, ethertype `0x0809` (filtr `ether proto 0x0809 and ether src not <vlastní MAC>`), a `0x8008` – **NetPanel neimplementuje** |

## Rámec

```
<XML dokument v UTF-8, řádky \r\n>  +  checksum (2 bajty, little-endian)
```

Checksum (`FUN_007a0430`): součet 16bitových slov **little-endian** přes všechny bajty XML (lichý poslední bajt
se přičte jako nižší bajt), přenos se dvakrát přičte zpět, výsledek se neguje (`~sum & 0xFFFF`) – tedy
klasický „Internet checksum“ (RFC 1071) nad LE slovy. Přijímač (`FUN_007af710`) spočítá checksum nad
`délka − 2` bajty a porovná s posledními dvěma; při neshodě zprávu zahodí („checkSum error“).

XML skládá CMarkup: hlavička `<?xml version="1.0" encoding="UTF-8"?>\r\n`, každý prvek na vlastním řádku,
bez odsazení. Struktura je plochá (kořen + děti).

## Zprávy

**Hledání kamer** (`FUN_007aa230`, buffer 256 B):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<Query>
<Id>dca6324b7e01-12345678-9ABC-def0-1122-334455667788</Id>
<Types>search</Types>
</Query>
```

Pro NVR navíc `<DeviceTypes>NVR</DeviceTypes>`. `Id` = MAC adaptéru (12 hex, malými) + GUID ve formátu
`%08X-%04X-%04x-%02X%02X-%02X%02X%02X%02X%02X%02X` (`FUN_007ad580`). Kamera `Id` vrací; Smart Tools z něj
přes `sscanf("%02x…")` kontroluje jen MAC část.

**Odpověď** (kořen `Reply`, parsování `FUN_007af710`): `Id`, `Types` (`search`), `DeviceTypes` (`NVR` / jiné =
kamera), `MAC`, a dále pro kameru: `IPv4Address`, `HttpPort`, `HttpEnable`, `HttpsEnable`, `HttpsPort`,
`IPv4SubnetMask`, `IPv4Gateway`, `DNSIp`, `Model`, `Software`, `Hardware`, `DeviceName`, `ActiveStatus`,
`OemModel`, `Runtime`, `SN`, `PwdStrengthType`; pro NVR: `IPv4Address`, `HttpPort`, `IPv4SubnetMask`,
`IPv4Gateway`, `DNSIp`, `Model`, `Software`, `OemModel`, `DeviceName`, `Runtime`, `UpdateTime`, `ActiveStatus`.
`ActiveStatus` prázdný ⇒ Smart Tools bere jako 1; `0` = neaktivovaná kamera (bez hesla).

**Změna IP** (`FUN_007a1fd0`, buffer 1 kB, posílá se stejným multicastem – adresuje se MAC adresou):

```xml
<Query>
<Id>…</Id>
<Types>modify</Types>
<MAC>1c:c3:16:2a:10:64</MAC>
<IPv4Address>192.168.10.64</IPv4Address>
<HttpPort>80</HttpPort>
<IPv4SubnetMask>255.255.255.0</IPv4SubnetMask>
<IPv4Gateway>192.168.10.1</IPv4Gateway>
<DNSIp>8.8.8.8</DNSIp>
<DeviceName>…</DeviceName>
<UserName>admin</UserName>
<Password>…MD5(heslo) hex, malými…</Password>
</Query>
```

Heslo: `FUN_007ad700` → `QCryptographicHash(Md5)` nad `QString("%1").arg(heslo)` převedeným do UTF-8,
`toHex()`. Odpověď: `<Reply>` s `Types` = `modify`, `MAC` a `Result` = `success` / `failed`.

Další typy, které Smart Tools zná, ale NetPanel ne: `challenge` (`KeyStatu`, `KeyID`, `ChallengeKey`),
`activate` (`Status`), `SetSqaChallenge`, `SetSQA`, `QAChallenge` – aktivace a bezpečnostní otázky
(s veřejným RSA klíčem, `PublicKey`).

## ONVIF

Smart Tools používá i standardní ONVIF WS-Discovery (`MsIpcSDK.dll`, gSOAP, Probe na `NetworkVideoTransmitter`).
NetPanel posílá Probe pro `dn:NetworkVideoTransmitter` a `tds:Device` na `239.255.255.250:3702`.

## Jak ověřit na reálné kameře

```bash
cd /opt/netpanel && sudo -u netpanel python3 -m netpanel.backend.cameras eth0 --debug
sudo tcpdump -i eth0 -n -X 'udp port 26360'
```

Pokud odpověď přijde jinak (unicast, jiný kořen, jiný checksum), `--debug` výstup ukáže hex i XML každého paketu.
