# LabVIEW patch guide for synchronized two-OECT acquisition

## What is slow in the current VI

`YZ_Stair case 2sim lvl_26.09.15.vi` contains four separate Select Channel / Read Output / Read Single Point paths on one VISA session. The shared VISA and error wires serialize them. Each read therefore starts after the previous channel has completed; putting the four nodes into parallel LabVIEW loops would not make one LAN/GPIB session or the instrument execute four independent reads simultaneously.

The timing problem should be fixed inside the ADCMT 6541:

- CH1: timing master, OECT A drain
- CH2: synchronized slave, OECT A gate
- CH3: synchronized slave, OECT B drain
- CH4: synchronized slave, OECT B gate
- All channels: DC voltage source and DC current measurement
- Sampling: 1 measurement per common hardware trigger, 1000 ms master period
- Retrieval: measurement-memory address ranges, fetched in batches

The hardware specification gives an approximately 5 us master-to-slave synchronization delay. Compensating the master measurement delay by +5 us aligns the measurement starts. This is the closest the instrument supports to "exactly the same time."

## Recommended block-diagram architecture

Use one state machine rather than four sequential measurement sub-VIs:

1. `Initialize`
2. `Configure all channels (Standby)`
3. `Operate master`
4. For each gate-voltage step:
   - Pause master triggering (`SCH1`, `M1`).
   - Change CH2 and CH4 source values.
   - On every channel: `ST0`, `RL`, `RNM 3600`, `ST1`.
   - Start the master timer (`SCH1`, `M0`).
   - Periodically read `SZ?`; fetch only new common ranges with `RDN` + `RDT?`.
   - Append the four channel arrays to disk using the same sample index.
5. `Standby master`
6. `Close`

The ADCMT driver archive already in the folder contains useful building blocks:

- `Examples/ADCMT 6541 Read Multiple by 4 Channels.vi`
- `Configure Control Signals.vi`
- `Configure Trigger.vi`
- `Configure Timing (DC).vi`
- `Configure Multiple Points.vi`
- `Read (Multiple Points).vi`

The official four-channel example is useful for the memory-fetch pattern, but its comment says it applies the same settings sequentially. For this experiment, keep your independent CH1-CH4 voltage/limit settings and add the synchronization settings below.

## Exact per-channel settings

The most direct LabVIEW implementation is to send these commands with VISA Write during initialization. Semicolons may combine commands on one line, but separate writes are easier to debug.

### CH1 - master

```text
SCH1
SBY
MD0
VF
SVR4
LMI<CH1 current limit in A>
SOV<CH1 Vds in V>
FX0
F2
R1
IT5
AZ0
RE5
SPN1
SP0,0.055,1000
SD0.035
OP3
CP6
TG0
M1
```

### CH2, CH3, CH4 - slaves

Repeat for each slave with its own `SOV` and `LMI`:

```text
SCH<n>
SBY
MD0
VF
SVR4
LMI<channel current limit in A>
SOV<channel source voltage in V>
FX0
F2
R1
IT5
AZ0
RE5
SPN1
SP0,0.050,999
SD0.030
OP1
CP7
TG1
M1
```

Global response-format commands:

```text
DL1
OH0
```

After all four channels are configured, `SCH1` followed by `OPR` puts all four outputs into Operate through the 6541's internal synchronization wiring. `SCH1` followed by `M0` starts the one-second hardware timer. The slaves remain in `M1` HOLD mode and measure from CH1's internal `SYNC OUT` pulse.

## Data-memory loop

At the start of each gate step, while CH1 is in `M1` HOLD:

```text
SCH1   ST0   RL   RNM3600   ST1
SCH2   ST0   RL   RNM3600   ST1
SCH3   ST0   RL   RNM3600   ST1
SCH4   ST0   RL   RNM3600   ST1
SCH1   M0
```

Do not call four `Read (Single Point)` VIs every second. Instead:

1. Query `SZ?` periodically.
2. When at least 60 new addresses are available, find the minimum stored count across CH1-CH4.
3. For each channel, send `RDN<first>,<last>` and query `RDT?`.
4. Combine the four arrays by address and write them as one batch.

The acquisition continues on the 6541 while LabVIEW reads an older memory range, so host/network delays do not change the 1.000 s interval. Keep all ADCMT I/O in one producer loop; pass completed arrays to a separate file/graph consumer loop by queue.

## Range and safety notes

- `R1` makes the current-measurement range follow the current-limit range for voltage sourcing. Choose each `LMI` carefully. The example configuration preserves the original VI's 0.3 A values only as placeholders; lower them to a safe range for the OECTs before energizing the outputs.
- Fixed source and measurement ranges avoid variable auto-ranging delays and missing data.
- `IT5` is 1 PLC. A different integration time is possible, but it must fit comfortably inside the 1 s period.
- CH1 and CH3 Vds may differ; CH2 and CH4 gate voltages may also differ. Synchronization controls timing, not source-value equality.
- The 6541 does not output its own measurement timestamp. Treat the common memory address as the authoritative sample index and store a host UTC time for each step start.

## Manual locations

The folder contains both language editions:

- Japanese: `../6540-41-ope-foj-00000142B00-j.pdf`
- English: `../6540-41-ope-foe-00000147b01-e.pdf`

Relevant English-manual sections are 2.9.2 (multiple-channel synchronization), 4.2.10.1 (internal control-signal wiring), 4.2.11 (approximately 5 us synchronization delay), 4.2.12 (10,000-point/channel memory), 5.6 (data format), 5.7.3 (`SP`, `TG`, `CP`, `OP`, `ST`, `RDN`, `RDT?`, `SZ?`), and Appendix A.3 (LAN transfer timing).
