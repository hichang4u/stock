# 운영 PC 시계 동기화 복구 기록 (2026-11-13)

`docs/CLOCK_DRIFT_FOLLOWUP_20261113.md`의 후속이다. 조사·측정은 2026-11-13(금) 17:08 ~ 17:27 로컬 시각에 했다. 시스템 날짜와 KST
날짜가 같다. 장 마감 뒤다.

**결과: PC 시계는 아직 복구되지 않았다.** 이 세션은 관리자 권한이 없어 시간 서비스를 시작하지 못했다. 원인은 확정했고, 사용자가
관리자 PowerShell에서 실행할 명령 다섯 줄을 3절에 적었다. 실행 뒤의 확인 절차는 4절이다.

이 문서의 시각은 로컬 시각이다. 로컬 시계는 실제보다 약 2.7초 늦다(1.3절).

## 1. 변경 전 상태 (실측)

### 1.1 안전 조건

| 항목 | 값 | 출처 |
|---|---|---|
| 현재 시각 | 2026-11-13 금요일 17:08 KST | `Get-Date`, 표준 시간대 `Korea Standard Time` |
| 장 | 마감 뒤 | — |
| 운영 봇 | 실행 중(`main.py`, 2026-11-12 08:00:04 시작) | `Win32_Process` |
| 포지션 | 없음. `trades` 미청산 0건, 오늘 거래 `CLOSED`(`HARD_STOP`, 39주) | 운영 DB `mode=ro` |
| 미체결 주문 | 없음. 오늘 주문 2건 모두 `FILLED`(39/39) | 운영 DB `mode=ro` |
| 운영 로그 마지막 줄 | 15:29:22 `TRACK_B_BARS_CORRECTED` | `data/logs/20261113.jsonl` |
| 남은 예약 작업 | 다음은 다음 거래일 08:29:30 토큰 갱신, 08:30:10 시각 점검 | `src/scheduler.py` |

KIS API·인증·주문 호출은 하지 않았다. `db.init`도 부르지 않았다. 운영 트리와 부모 트리(`main`, `b893149`)는 읽기만 했다.

### 1.2 시간 서비스

| 항목 | 값 |
|---|---|
| OS | Windows 11 Pro 26300, 마지막 부팅 2026-11-12 03:02:20 |
| 도메인 | 가입 안 함(`PartOfDomain=False`) |
| 그룹 정책 | 없음(`HKLM\SOFTWARE\Policies\Microsoft\W32Time` 키 없음) |
| 서비스 상태 | `STOPPED`, `WIN32_EXIT_CODE 1077`(부팅 뒤 시작 시도가 한 번도 없었다) |
| 시작 유형 | `DEMAND_START`(수동), `Start=3`, 계정 `NT AUTHORITY\LocalService` |
| 시작 트리거 | 도메인 가입 상태, 사용자 지정 시스템 상태 변경 이벤트 1개(기본 등록값) |
| 실패 시 동작 | 60초 뒤 재시작, 120초 뒤 재시작 |
| 소스 | `Type=NTP`, `NtpServer=time.windows.com,0x9` |
| 폴링 | `MinPollInterval=10`(1,024초), `MaxPollInterval=15`, `SpecialPollInterval=16384`초 |
| 보정 한도 | `MaxAllowedPhaseOffset=1`초, `MaxPosPhaseCorrection`·`MaxNegPhaseCorrection`=54,000초 |
| 예약 작업 `SynchronizeTime` | 사용 가능. 동작 `sc.exe start w32time task_started`, 계정 `LOCAL SERVICE`. 마지막 실행 2026-11-11 22:54:26(결과 0) |
| 예약 작업 `ForceSynchronizeTime` | 사용 가능. 마지막 실행 2026-11-12 03:02:33(결과 0, 부팅 13초 뒤) |
| 시간 서비스 이벤트(System) | 마지막 2026-11-12 00:02:59(ID 158). ID 37(유효한 시간 수신)은 2026-11-11 22:54:27이 마지막 |
| 시스템 시각 변경 이벤트(Kernel-General 1) | 마지막 2026-11-11 22:38:59(+4.8ms). 그 뒤로 아무도 시계를 고치지 않았다 |

- 값은 모두 Windows 기본값이다. Microsoft 문서의 독립 실행형 클라이언트 기본값과 같다(`MaxAllowedPhaseOffset` 1, `MinPollInterval` 10,
  `MaxPollInterval` 15, `MaxPosPhaseCorrection` 54,000, `NtpServer` `time.windows.com`).
- 서비스 권한(`sc sdshow`): 대화형 사용자에게는 조회 권한만 있다. 시작 권한은 `SYSTEM`, `Administrators`, `LOCAL SERVICE`에만 있다.
- 예약 작업 권한: 사용자에게는 읽기뿐이다(`FR;;;AU`).

### 1.3 현재 오차

`w32tm /stripchart /computer:<서버> /samples:N /dataonly`로 쟀다. 이 명령은 서비스 없이도 돈다. 양수는 서버가 앞선다는 뜻이다.

| 로컬 시각 | 서버 | 표본 | 값 |
|---|---|---|---|
| 17:11:32 ~ 38 | `time.windows.com` | 4 | +2.6669 ~ +2.6704초 |
| 17:11:38 ~ 44 | `time.google.com` | 4 | +2.6686 ~ +2.6701초 |
| 17:22:18 ~ 23 | `time.windows.com` | 3 | +2.6851 ~ +2.6859초 |
| 17:22:23 ~ 27 | `time.google.com` | 3 | +2.6858 ~ +2.6859초 |
| 17:22:27 ~ 31 | `time.cloudflare.com` | 3 | +2.6858 ~ +2.6863초 |
| 17:22:51 ~ 57 | `time.windows.com` | 3 | 타임아웃 2회(`0x800705B4`), +2.6861초 |
| 17:22:57 ~ 23:01 | `time.google.com` | 3 | +2.6861 ~ +2.6879초 |
| 17:23:01 ~ 05 | `time.cloudflare.com` | 3 | +2.6866 ~ +2.6866초 |
| 17:26:23 ~ 27 | `time.google.com` | 3 | +2.6893 ~ +2.6913초 |
| 17:26:27 ~ 32 | `time.cloudflare.com` | 3 | +2.6913 ~ +2.6917초, 타임아웃 1회 |

- **로컬 시계가 약 2.69초 늦다.** 세 소스가 0.003초 안에서 일치한다. 봇이 쓰는 `ntplib`으로도 같은 값이 나왔다(`time.google.com`
  +2.6737초, `time.windows.com` +2.6740초, 17:14 무렵). `ntplib`의 offset은 서버 시각 − 로컬 시각이다.
- 17:11 → 17:22에 0.016초, 17:26까지 0.021초 늘었다. 한 시간에 약 0.09초다. 일일 보고의 틱 기준 값(0.07 ~ 0.08초/시)과 맞는다.
- `time.windows.com`은 한 번의 3표본 가운데 2표본이 타임아웃이었다. 다른 두 서버는 타임아웃이 없었다.
- 그대로 두면 2026-11-16(월) 08:30에 약 7.8초다(추정, 선형 외삽).

## 2. 원인 (확정)

**2026-11-10 10:18에 시간 서비스가 해제됐다가 다시 등록되면서 설정이 전부 기본값으로 돌아갔다.** 기본값에서는 서비스가 수동
시작이라 재부팅 뒤에 스스로 뜨지 않는다.

| 시각 | 사실 | 출처 |
|---|---|---|
| 2026-09 무렵(추정) | 서비스를 자동 시작으로 바꾸고, 소스와 보정 한도를 따로 설정했다 | 사용자 PowerShell 입력 기록(원문은 싣지 않는다) |
| ~ 11/10 10:18:08 | 서비스(`svchost.exe` PID 4000)가 16초마다 시계를 고쳤다. 11/08 7,782건, 11/09 10,660건, 11/10 4,633건 | Kernel-General ID 1 |
| 11/10 09:53 | 다른 조사에서 이 잦은 시각 변경이 System 로그를 채우는 것이 눈에 띄었다 | 세션 기록 |
| **11/10 10:18:22** | **"Windows Time" 서비스 설치, 시작 유형 "요청 시 시작". 실행 계정은 이 PC의 사용자 계정** | Service Control Manager ID 7045 |
| 11/10 10:18:29 ~ 36 | 서비스 시작, `time.windows.com`에서 유효한 시간 수신·동기화 | Time-Service ID 37, 35 |
| 11/10 14:51:40, 11/11 22:54:27 | 유효한 시간 수신(서비스가 가끔 떴다) | Time-Service ID 37 |
| 11/11 22:54:26 | `SynchronizeTime` 예약 작업의 마지막 실행 | 작업 스케줄러 |
| 11/12 00:02:59 | 시간 서비스의 마지막 이벤트 | Time-Service ID 158 |
| 11/12 03:02:20 | 재부팅(Windows 업데이트 `KB5132061`, `KB5132688`) | `LastBootUpTime`, `Get-HotFix` |
| 11/12 03:02:33 | `ForceSynchronizeTime` 실행. 11/12 08:00 점검이 40.5ms, 08:30이 2.4ms였던 까닭으로 본다(추정) | 작업 스케줄러 |
| 그 뒤 | 서비스 시작 시도 0회(종료 코드 1077). 시계는 한 번도 고쳐지지 않았다 | `sc queryex`, Kernel-General ID 1 |

- 입력 기록에는 서비스 중지 → `w32tm /unregister` → `w32tm /register` → 서비스 시작 → 재동기화 순서가 남아 있다. 7045 이벤트의
  시각·계정과 맞는다.
- Microsoft 문서: `/unregister`는 "Unregisters W32Time and removes all its configuration information from the registry", `/register`는
  "Registers W32Time to run as a service and adds its default configuration information to the registry"다. 그래서 자동 시작과
  사용자 설정이 함께 사라졌다.
- 기본값에서 서비스를 띄우는 것은 트리거와 `SynchronizeTime` 예약 작업뿐이다. 그 작업은 자동 유지 관리에 묶여 있고(`Period P1D`,
  `Deadline P1DT1H`) 11/12 재부팅 뒤 한 번도 돌지 않았다. 왜 돌지 않았는지는 보지 않았다.
- 이 PC의 시계는 고쳐 주지 않으면 한 시간에 0.07 ~ 0.09초 늦어진다. 11/10 10:18까지는 서비스가 16초마다 고쳐서 08:30 점검이
  5.8ms 이하였다.

출처: [Windows Time service tools and settings](https://learn.microsoft.com/en-us/windows-server/networking/windows-time-service/windows-time-service-tools-and-settings)
(Microsoft Learn, 2026-11-13 확인).

## 3. 복구 — 시도와 필요한 명령

### 3.1 이 세션에서 한 시도

| 로컬 시각 | 명령 | 결과 |
|---|---|---|
| 17:12:01 | `sc.exe start w32time` | `[SC] StartService: OpenService FAILED 5: Access is denied.` (종료 코드 5) |
| 17:12 | `w32tm /resync` | `0x80070426`(서비스가 시작되지 않음) |
| 17:12 | `sc.exe query w32time` | `STOPPED`, 종료 코드 1077 그대로 |

- 세션 토큰: `BUILTIN\Administrators`가 "거부 전용", 무결성 수준 "중간"이다. 관리자 권한이 없는 토큰이다.
- UAC 승격, 예약 작업을 통한 우회, 서비스 권한 변경은 시도하지 않았다. `Set-Date`도 쓰지 않았다.
- **바뀐 것은 없다.** 서비스 상태·시작 유형·소스·시계 모두 1절 그대로다. 17:26에 다시 봤을 때도 서비스는 중지·수동이고 마지막
  시각 변경 이벤트는 11/11 22:38:59 그대로였다.

### 3.2 사용자가 실행할 명령 (관리자 PowerShell)

```powershell
sc.exe config w32time start= auto     # 재부팅 뒤에도 뜨게 한다. 9월에 쓰던 설정과 같다
sc.exe start w32time                  # 서비스를 시작한다
w32tm /resync                         # 바로 다시 맞춘다. 끝날 때까지 기다린다
w32tm /query /status                  # 마지막 동기화 시각, 소스, Phase Offset을 본다
w32tm /query /source                  # 소스가 time.windows.com인지 본다
```

- 소스는 바꾸지 않는다(`time.windows.com,0x9`). 지금도 응답한다(1.3절).
- 트리거, 예약 작업, 레지스트리 값, 표준 시간대는 건드리지 않는다. 서비스 해제·재등록도 하지 않는다. 재부팅도 필요 없다.
- **장중(08:00 ~ 15:30)에는 하지 않는다.** 지금(장 마감 뒤, 포지션 없음)이나 주말이 맞다.
- **시계가 한 번에 약 2.7초 앞으로 뛴다.** 오차가 `MaxAllowedPhaseOffset`(1초)을 넘어 서비스가 시계를 바로 고친다
  (Microsoft 문서: 오차가 이 값을 넘으면 "W32Time sets the computer clock directly").
- 원복: `sc.exe config w32time start= demand` 다음 `sc.exe stop w32time`. 시계를 되돌리지는 않는다.
- `/resync`가 "시간 데이터를 사용할 수 없습니다"로 끝나면 `w32tm /resync /rediscover`로 한 번 더 한다.

### 3.3 필요하면 더할 것 (지금은 권하지 않는다)

기본 플래그 `0x9`는 `SpecialPollInterval`(16,384초, 약 4.6시간) 간격으로 묻는다. 11/10 전에는 플래그 `0x8`로 1,024초마다 물었고
08:30 점검이 5.8ms 이하였다. 기본값으로 서비스가 계속 떠 있을 때 이 PC가 ±200ms를 지키는지는 잰 적이 없다.

- 3.2절을 실행한 뒤에도 오차가 200ms를 넘으면 플래그만 바꾼다. 소스는 같다.

```powershell
w32tm /config /manualpeerlist:"time.windows.com,0x8" /syncfromflags:manual /update
w32tm /resync
```

- 원복: `w32tm /config /manualpeerlist:"time.windows.com,0x9" /syncfromflags:manual /update`.
- 9월 설정의 `MaxAllowedPhaseOffset=0`은 되살리지 않는다. 16초마다 시계를 바로 고쳐 System 로그를 채운 것이 그 설정이다(추정).

## 4. 실행 뒤 확인 (아직 하지 않았다)

관리자 권한 없이 읽을 수 있는 것만 적었다. `w32tm /query`는 관리자 권한이 필요하다.

```powershell
Get-Service w32time | Select-Object Status, StartType          # Running / Automatic
sc.exe queryex w32time                                         # STATE 4 RUNNING, PID
w32tm /stripchart /computer:time.google.com /samples:5 /dataonly
w32tm /stripchart /computer:time.cloudflare.com /samples:5 /dataonly
Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Time-Service'} -MaxEvents 5
Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-General'; Id=1} -MaxEvents 3
```

복구됐다고 적으려면 다음이 모두 맞아야 한다.

1. 서비스가 `Running`이고 시작 유형이 `Automatic`이다.
2. 두 소스 이상에서 오차가 ±200ms 안이다. 수십 초 간격으로 두 번 이상 잰다.
3. System 로그에 그 시각의 Time-Service ID 37 또는 35, 그리고 Kernel-General ID 1(약 +2.7초)이 있다.
4. 한참 뒤(다음 폴링 뒤)에도 서비스가 떠 있고 오차가 유지된다. 한 번 맞은 것만으로는 지속 동기화가 확인된 것이 아니다.

- **재부팅 뒤 지속성은 설정으로만 확인할 수 있다.** 시작 유형이 `Automatic`이면 부팅 때 뜬다. 실제 재부팅은 하지 않으므로
  "재부팅 검증 안 함"으로 남긴다.
- 다음 거래일 08:30:10의 `TIME_SYNC_OK`(`offset_ms` ≤ 200)가 봇 쪽 확인이다. 봇의 `ntp_level`은 그때까지 `CRIT`으로 남는다
  (하루 한 번과 시작 때만 잰다).

## 5. 시계를 고칠 때 생기는 불연속과 측정 경계

- **고치는 순간 로컬 시각이 약 2.7초 건너뛴다.** 정확한 앞뒤 값은 Kernel-General ID 1 이벤트에 남는다. 그 시각이 경계다.
- 경계 앞의 자료는 모두 늦은 시계로 찍혔다: 11/12 08:30 ~ 경계까지의 운영 로그 `ts`, 틱 `received_at`, 분봉 기록 시각, DB의
  `ordered_at`·`filled_at`·`exit_at`, PC 프로브 기록. 늦은 양은 11/12 0 ~ 0.5초, 11/13 2.0 ~ 2.7초다. 고쳐 쓰지 않는다. 읽을 때
  감안한다.
- 거래소 시각(틱 `source_ts`, 통보 `exchange_time`)과 구간 길이(`*_ms`, 하트비트 간격)는 영향이 없다. 봇의 지연 측정은
  `time.monotonic()`을 쓴다.
- 경계 뒤 첫 거래일의 틱에서 수신 시각 − 거래소 시각의 최솟값이 0 근처로 돌아와야 한다(일일 보고 1절의 명령).
- 봇은 재시작하지 않는다. 예약 작업은 로컬 시계를 따르므로 고친 뒤에는 제시각에 돈다. 장 마감 뒤에는 그 2.7초 안에 걸리는 예약
  작업이 없다.
- 이 조사에서 쓴 시각(17:08 ~ 17:27)도 늦은 시계 기준이다.

## 6. 재현 명령 (모두 읽기 전용)

```powershell
Get-Service w32time | Select-Object Name, Status, StartType
sc.exe qc w32time; sc.exe qtriggerinfo w32time; sc.exe queryex w32time; sc.exe qfailure w32time; sc.exe sdshow w32time
Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\Parameters'
Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\Config'
Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Services\W32Time\TimeProviders\NtpClient'
Test-Path 'HKLM:\SOFTWARE\Policies\Microsoft\W32Time'
(Get-CimInstance Win32_ComputerSystem).PartOfDomain
schtasks /query /tn "\Microsoft\Windows\Time Synchronization\SynchronizeTime" /xml
Get-ScheduledTask -TaskPath '\Microsoft\Windows\Time Synchronization\' | Get-ScheduledTaskInfo
Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Time-Service'; StartTime=(Get-Date '2026-11-01')}
Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Service Control Manager'; Id=7045; StartTime=(Get-Date '2026-11-08')}
Get-WinEvent -FilterHashtable @{LogName='System'; ProviderName='Microsoft-Windows-Kernel-General'; Id=1} -MaxEvents 5
w32tm /stripchart /computer:time.windows.com /samples:4 /dataonly
w32tm /stripchart /computer:time.google.com /samples:4 /dataonly
whoami /groups
```

System 로그는 2026-11-08 06:41부터만 남아 있다(크기 한도). 그 앞의 시간 서비스 기록은 볼 수 없다.
