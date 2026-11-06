# OECT GUI programs

## Dual OECT program

Open `launch_gui.command` for the four-channel dual-OECT program.

Each OECT card has its own ON/OFF switch:

- **ON:** that OECT follows its voltage schedule, writes its own raw-data file, and updates its two live plots.
- **OFF:** both source settings for that OECT remain at 0 V for the full run. It creates no raw-data file and receives no plot samples.

At least one OECT must be ON. If only OECT B is ON, CH1 remains operating at 0 V because it supplies the ADCMT 6541 synchronization clock; OECT A is still held at 0 V and is not logged.

## Single OECT program

Open `launch_single_gui.command` for the separate one-OECT program.

- CH1: drain/source voltage and drain-current measurement; synchronization master.
- CH2: gate voltage and gate-current measurement; synchronized slave.
- CH3 and CH4: placed in standby with their external synchronization disabled.

The single-OECT front panel uses the same voltage/timing controls, cream theme, inverted vertical plot axes, raw-data format, overwrite confirmation, and simulation mode as the dual program. Its default output is `measurement=single.txt` in the `ADCMT IV` mother folder.

Both programs use the 6541's `R0` automatic current-measurement range with `RD0` range-change delay. The displayed 0.3 A value is the maximum compliance, not a fixed 300 mA measurement range. The instrument can therefore move down to its 3 microampere range, which has 10 pA resolution, instead of quantizing measurements to 1 microampere steps on the 300 mA range.

Both programs set the active OECT's Vds and Vg to 0 V after its schedule finishes. Before a live experiment, validate the wiring and stop behavior with a short dummy-load run.

## Back-and-forth sweeps

Both programs have a **Program** selector with two modes:

- **Rise and keep:** the original one-way sweep from Vg initial to Vg final. **Keep at final** (default 3600 s) extends the final measurement period; recording continues during the keep.
- **Sweep in cycles:** repeated initial → final → initial sweeps. **Cycles** (default 1) sets the number of round trips, with no extra final hold. This is the default selected mode.

The relevant Keep at final or Cycles control appears when its mode is selected. Switching modes preserves both values. Each OECT in the dual program can independently select its mode and settings. Both programs zero the sources after their chosen schedule finishes.

For Vg initial = 0 V, Vg final = -0.3 V, Vg step = -0.1 V and Cycles = 2, the measured levels are:

`0, -0.1, -0.2, -0.3, -0.2, -0.1, 0, -0.1, -0.2, -0.3, -0.2, -0.1, 0 V`

In Sweep in cycles mode, every level uses the configured Period, including the final return to initial. Delay remains inside each period. There is no additional hold after completion. Total duration is the number of listed levels multiplied by Period (13 periods in this example). If the endpoints are equal, there is a single measurement period. If the step does not divide the range exactly, the final endpoint is included and the return retraces the same levels.
