# Foamy Vitals

CPU, memory, GPU, temperatures, processes, network traffic and disk capacity
for Omarchy Quattro.

## Install locally

Link this folder as `~/.config/omarchy/plugins/foamy.vitals`, then enable it:

```sh
omarchy plugin enable foamy.vitals
omarchy restart shell
```

## Use

- Click the bar reading to open the panel. The adaptive bar shows the highest
  available CPU, RAM or GPU percentage.
- Open the cog for language, bar display and warning settings. Settings use
  Omarchy's widget API and are saved in the widget's `shell.json` entry.
- Right-click the bar, click **btop**, or press **B** to open btop.
- **S** opens settings; **Esc** goes back or closes the panel. **R** samples again.

The panel shows all details without an expander. Readings refresh every two
seconds while open and every five seconds while closed. Graphs retain two
minutes of samples. Network traffic uses solid lines around a centered zero:
incoming above, outgoing below, on the same scale. Both graphs place their
Y-axis labels beside the plot. Disk capacities refresh on
opening and every minute.
The hostname icon follows the system chassis type reported by `hostnamectl`.

CPU, RAM, GPU and VRAM warn at 95% by default, with independent thresholds.
CPU and GPU temperatures warn 15°C below their own driver-reported critical
limits by default, with independent margins. Any of these six warnings turns
the bar red. Unknown limits stay explicit and do not produce guessed warnings.

CPU temperature uses the package sensor. GPU temperature selects the sensor
closest to its own limit; its label identifies edge, hotspot or memory.
Unavailable readings show `--`. Network traffic follows the lowest-metric IPv4
default-route interface; process CPU percentages use 100% per thread. Mounted
subvolumes sharing a device are listed once, preferring the root mount.

## Requirements

Omarchy Quattro and its existing Bash, awk, coreutils and util-linux tools.
btop is optional. The sampler reads `/proc` and sysfs without elevated access
or network connections. Hardware readings depend on the installed drivers.
GPU utilization supports the standard DRM busy counter and the existing Intel
RC6 fallback; VRAM requires driver-provided counters.

## Validation

```sh
node --test --test-isolation=none tests/*.test.js
bash -n stats.sh
shellcheck stats.sh
qmllint -I /usr/share/omarchy/shell ./*.qml
omarchy plugin validate .
```

## License

[MIT](LICENSE), retaining the original Vitals attribution. Omarchy and Lucide
notices are in [LICENSE-OMARCHY](LICENSE-OMARCHY) and [LICENSE-LUCIDE](LICENSE-LUCIDE).
