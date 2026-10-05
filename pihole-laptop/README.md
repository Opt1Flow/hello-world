# Pi-hole on an old Windows laptop (Windows stays usable)

Goal: an old laptop sits at another location, stays on, and runs **Pi-hole**
(network-wide ad/tracker blocking) on **Linux**, while you can still sit down
and use **Windows** on it whenever you want.

## 1. Pick the right architecture: VM, not dual-boot

| Approach | Windows and Pi-hole at the same time? | Verdict |
|---|---|---|
| Dual-boot (choose Windows *or* Linux at startup) | No: booting Windows turns Pi-hole off, so DNS for the whole network breaks | Not a fit |
| **Linux VM inside Windows (bridged network)** | Yes | **Recommended** |
| Docker Desktop / WSL2 container | Yes, but UDP port 53 forwarding to the LAN is fragile | Possible, harder to debug |

**Why this matters.** Pi-hole is a *DNS server*. Every device on the network
asks it "what IP is `example.com`?" on **port 53**. If that server goes away,
nothing on the network can resolve names, so "the internet is down" for everyone.
That means Pi-hole has to run all the time, which rules out dual-boot.

A virtual machine (VM) lets the laptop run two operating systems at once:
Windows is the **host**, and a small Linux system runs as the **guest** in a window
or in the background.

### Bridged vs NAT networking (the key concept)

* **NAT** (the default in most VM tools): the VM sits *behind* Windows, like a
  device behind a second router. Other devices on the LAN **can't reach it**
  directly, so Pi-hole wouldn't work.
* **Bridged**: the VM's virtual network card is connected straight onto the same
  LAN as the laptop. The router sees it as a **separate device with its own IP
  and MAC address**. That's what Pi-hole needs.

EE analogy: bridged mode is like tying the VM onto the same node/bus as every
other device. NAT is like putting it behind a buffer stage that only lets
signals out, not in.

## 2. What you need

* Laptop with a 64-bit CPU and **virtualization enabled in BIOS/UEFI**
  (Intel VT-x / AMD-V, sometimes called "SVM"). Check it in
  Task Manager → Performance → CPU → "Virtualization: Enabled".
* At least 4 GB of RAM total (the Pi-hole VM only needs about 512 MB to 1 GB).
* About 15 GB of free disk space.
* **Ethernet strongly preferred.** Bridging over Wi-Fi often works, but some
  Wi-Fi drivers and access points reject the extra MAC address.
* Linux ISO: **Debian 12 (netinst)** or **Ubuntu Server 24.04 LTS**. Both are
  officially supported by Pi-hole and both are lightweight.

Which hypervisor depends on your Windows edition (Settings → System → About):

* **Windows Pro/Enterprise/Education** → use **Hyper-V** (built in, and it
  auto-starts VMs at boot natively). See 3A.
* **Windows Home** → use **VirtualBox** (free). See 3B.

## 3A. Hyper-V (Windows Pro)

1. Enable Hyper-V: open PowerShell **as Administrator** and run
   `Enable-WindowsOptionalFeature -Online -FeatureName Microsoft-Hyper-V -All`,
   then reboot.
2. Open **Hyper-V Manager** → **Virtual Switch Manager** → New **External**
   switch → bind it to your Ethernet (or Wi-Fi) adapter. Keep
   "Allow management OS to share this network adapter" **checked** so Windows
   keeps internet access.
3. New → Virtual Machine:
   * Generation 2, 1024 MB RAM (turn **off** Dynamic Memory), 1 to 2 vCPUs
   * Network: the External switch from step 2
   * Disk: 16 GB, install from the Linux ISO
   * Settings → Security: for Ubuntu/Debian set the Secure Boot template to
     **"Microsoft UEFI Certificate Authority"** (or turn Secure Boot off)
4. VM Settings → **Automatic Start Action** → "Always start this virtual
   machine automatically". Hyper-V handles reboot survival by itself.

## 3B. VirtualBox (Windows Home)

1. Install VirtualBox from virtualbox.org.
2. New VM: name it **`pihole`** (the autostart script expects that name),
   type Linux / Debian or Ubuntu (64-bit), 1024 MB RAM, 1 CPU, 16 GB disk.
3. Settings → Network → Adapter 1 → **Attached to: Bridged Adapter** → pick
   your Ethernet/Wi-Fi card.
4. Install Linux from the ISO (you only need SSH server + standard system
   utilities, no desktop).
5. After Linux and Pi-hole are working, make the VM start headless at every
   boot: copy `windows/register-vm-autostart.ps1` to the laptop and run it in
   an **Administrator** PowerShell:
   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   .\register-vm-autostart.ps1 -VmName pihole
   ```

## 4. Give the Pi-hole VM a fixed IP

DNS servers must not change address, or every client loses them. The best way is a
**DHCP reservation** on the router: find the VM's MAC address (VM settings, or
`ip link` inside Linux) and reserve an IP such as `192.168.1.53`.
After that, the IP won't change across reboots and you won't need to edit Linux network
files.

## 5. Install Pi-hole (inside the Linux VM)

Copy `linux/install-pihole.sh` into the VM (or just run the official command
yourself):

```bash
curl -sSL https://install.pi-hole.net | sudo bash
sudo pihole setpassword          # set the web-admin password
```

The script also runs basic pre-flight checks (internet reachable, port 53 free,
which IP you're on). Afterwards, open `http://<vm-ip>/admin` from any device.

## 6. Point the network at Pi-hole

On the **router at that location**: LAN/DHCP settings → DNS server = the
Pi-hole IP (for example `192.168.1.53`). Reconnect devices (or wait for their
DHCP lease to renew) and they'll start using it.

Trade-off: if you add a public DNS server (like 1.1.1.1) as *secondary* DNS,
the internet stays up when the laptop is off, but clients will sometimes skip
Pi-hole, so some ads will get through. Pi-hole-only is cleaner. Just know that if the laptop
dies, change the router's DNS back.

## 7. Keep the laptop "always on" while still usable

In Windows:

* Settings → System → Power: **Sleep = Never** when plugged in.
* Control Panel → Power Options → "Choose what closing the lid does" →
  **Do nothing** (plugged in).
* Leave it plugged in. A side benefit is that the battery works as a small UPS, so
  Pi-hole survives short power blips.
* Windows Update: set **Active hours** so it doesn't reboot during the day. Reboots
  are fine anyway because the VM auto-starts (step 3A.4 / 3B.5).
* Using Windows normally is fine. The VM uses about 1 GB RAM and almost no CPU.
  The one thing to avoid is **shutting down / sleeping** Windows, since that pauses
  DNS for the network.

## 8. Managing it from somewhere else (optional)

Since the laptop is at another location, install **Tailscale** (free) inside the
Linux VM: `curl -fsSL https://tailscale.com/install.sh | sh && sudo tailscale up`.
You can then SSH in or open the admin page from anywhere without opening router ports.
**Never** port-forward port 53 to the internet, because an open resolver gets abused
for DDoS amplification.

## Troubleshooting

| Symptom | Likely cause / check |
|---|---|
| Other devices can't reach the Pi-hole IP | VM is on NAT, not Bridged/External switch |
| VM has no IP on Wi-Fi bridge | Wi-Fi driver/AP rejects extra MAC, so use Ethernet |
| VM won't start: "VT-x is disabled" | Enable virtualization in BIOS/UEFI |
| VirtualBox is very slow on a Pro machine | Hyper-V and VirtualBox fighting, so pick one |
| Ads still show | Client using a hard-coded DNS (e.g. Chrome "Secure DNS"/DoH), or a secondary DNS on the router |
| `nslookup example.com <pihole-ip>` from Windows fails | Pi-hole service down, run `pihole status` in VM |
