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

The single-OECT front panel uses the same voltage/timing controls, cream theme, inverted vertical plot axes, raw-data format, overwrite confirmation, simulation mode, and 0.3 A fixed current-limit warning as the dual program. Its default output is `measurement=single.txt` in the `ADCMT IV` mother folder.

Both programs set the active OECT's Vds and Vg to 0 V after its schedule finishes. Before a live experiment, validate the wiring and stop behavior with a short dummy-load run.
