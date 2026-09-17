#!/usr/bin/env python3
"""Interactively create an Ubuntu cloud-image based KVM VM with virt-install."""

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

IMAGE_DIR = Path("/var/lib/libvirt/images")
RELEASES = {
    "bionic": ("18.04 LTS", "ubuntu18.04"),
    "focal": ("20.04 LTS", "ubuntu20.04"),
    "jammy": ("22.04 LTS", "ubuntu22.04"),
    "noble": ("24.04 LTS", "ubuntu24.04"),
    "resolute": ("26.04 LTS", "ubuntu25.10"),
}
DEFAULT_RELEASE = "noble"
CONSOLES = {
    "terminal": "Default terminal emulator (new window)",
    "putty": "PuTTY",
    "remmina": "Remmina",
    "here": "ssh in this terminal",
    "none": "Do not open a console",
}
TERMINAL_EMULATORS = (
    "x-terminal-emulator",
    "gnome-terminal",
    "konsole",
    "xfce4-terminal",
    "tilix",
    "xterm",
)
NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$")


def base_image(release):
    return IMAGE_DIR / f"{release}-server-cloudimg-amd64.img"


def base_image_url(release):
    return (
        f"http://cloud-images.ubuntu.com/{release}/current/"
        f"{release}-server-cloudimg-amd64.img"
    )


def run(cmd, **kwargs):
    print("+ " + " ".join(str(c) for c in cmd))
    return subprocess.run(cmd, check=True, **kwargs)


def sudo(cmd):
    return cmd if os.geteuid() == 0 else ["sudo"] + cmd


def ask_name():
    while True:
        name = input("VM name: ").strip()
        if not NAME_RE.match(name):
            print("Invalid name: use letters, digits, '.', '-', '_' (max 63 chars).")
            continue
        exists = subprocess.run(
            ["virsh", "domstate", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode == 0
        if exists:
            print(f"A domain named '{name}' already exists.")
            continue
        return name


def ask_int(prompt, default, minimum, maximum):
    while True:
        raw = input(f"{prompt} [{default}]: ").strip() or str(default)
        try:
            value = int(raw)
        except ValueError:
            print("Please enter a whole number.")
            continue
        if not minimum <= value <= maximum:
            print(f"Please enter a value between {minimum} and {maximum}.")
            continue
        return value


def ask_release():
    releases = list(RELEASES)
    print("\nAvailable OS images:")
    for index, release in enumerate(releases, start=1):
        version, _ = RELEASES[release]
        cached = " (cached)" if base_image(release).exists() else ""
        print(f"  {index}) Ubuntu {version} ({release}){cached}")
    default = releases.index(DEFAULT_RELEASE) + 1
    choice = ask_int("Select OS image", default, 1, len(releases))
    return releases[choice - 1]


def ask_data_disk():
    if input("\nAdd an extra data disk? [y/N]: ").strip().lower() not in ("y", "yes"):
        return None
    while True:
        raw = input(f"Data disk file path [{IMAGE_DIR}/<name>-data.qcow2]: ").strip()
        if raw:
            path = Path(raw).expanduser()
            break
        path = None
        break
    size_gib = ask_int("Data disk size in GiB", 20, 1, 4096)
    return path, size_gib


def ask_console():
    consoles = list(CONSOLES)
    print("\nConsole to open after the VM boots:")
    for index, key in enumerate(consoles, start=1):
        available = key in ("terminal", "here", "none") or shutil.which(key)
        missing = "" if available else " (not installed)"
        print(f"  {index}) {CONSOLES[key]}{missing}")
    choice = ask_int("Select console", 1, 1, len(consoles))
    return consoles[choice - 1]


def ensure_base_image(release):
    image = base_image(release)
    if image.exists():
        return image
    url = base_image_url(release)
    print(f"Base image not found, downloading {url}")
    run(sudo(["wget", "-q", "--show-progress", url, "-O", str(image)]))
    return image


def ssh_public_key():
    for key in ("id_ed25519.pub", "id_rsa.pub"):
        path = Path.home() / ".ssh" / key
        if path.exists():
            return path.read_text().strip()
    return None


def write_cloud_init(vm_dir, name, pubkey):
    (vm_dir / "meta-data.yaml").write_text(
        f"instance-id: {name}\nlocal-hostname: {name}\n"
    )
    lines = ["#cloud-config", "users:", "  - name: ubuntu"]
    if pubkey:
        lines += ["    ssh_authorized_keys:", f"      - {pubkey}"]
    lines += [
        '    sudo: ["ALL=(ALL) NOPASSWD:ALL"]',
        "    groups: sudo",
        "    shell: /bin/bash",
        "",
    ]
    (vm_dir / "user-data.yaml").write_text("\n".join(lines))


def virsh(*args):
    result = subprocess.run(["virsh", *args], capture_output=True, text=True)
    return result.stdout if result.returncode == 0 else ""


def first_ipv4(text):
    for line in text.splitlines():
        match = re.search(r"(\d+\.\d+\.\d+\.\d+)/\d+", line)
        if match and not match.group(1).startswith("127."):
            return match.group(1)
    return None


def ip_from_leases(name):
    """Match DHCP leases by MAC; needed when the NIC is a raw bridge."""
    vm_macs = re.findall(
        r"([0-9a-f]{2}(?::[0-9a-f]{2}){5})", virsh("domiflist", name)
    )
    if not vm_macs:
        return None
    for net in virsh("net-list", "--name").split():
        for line in virsh("net-dhcp-leases", net).splitlines():
            if any(mac in line for mac in vm_macs):
                ip = first_ipv4(line)
                if ip:
                    return ip
    return None


def get_ip(name):
    for source in ("lease", "agent", "arp"):
        ip = first_ipv4(virsh("domifaddr", name, "--source", source))
        if ip:
            return ip
    return ip_from_leases(name)


def wait_for_ip(name, timeout=180, interval=5):
    print(f"Waiting up to {timeout}s for '{name}' to get an IP address...")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ip = get_ip(name)
        if ip:
            return ip
        time.sleep(interval)
    return None


def spawn(cmd):
    print("+ " + " ".join(cmd))
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def launch_console(console, ip, user="ubuntu"):
    target = f"{user}@{ip}"
    if console == "none":
        return
    if console == "here":
        subprocess.call(["ssh", "-o", "StrictHostKeyChecking=accept-new", target])
        return
    if console == "terminal":
        for term in TERMINAL_EMULATORS:
            path = shutil.which(term)
            if not path:
                continue
            sep = "--" if term in ("gnome-terminal", "tilix") else "-e"
            spawn([path, sep, "ssh", "-o", "StrictHostKeyChecking=accept-new", target])
            return
        print("No terminal emulator found; run: ssh " + target)
        return

    path = shutil.which(console)
    if path is None:
        print(f"{console} not found in PATH; run: ssh {target}")
        return
    if console == "putty":
        spawn([path, "-ssh", target])
    elif console == "remmina":
        spawn([path, "-c", f"ssh://{target}"])


def main():
    for tool in ("virsh", "virt-install", "qemu-img"):
        if shutil.which(tool) is None:
            sys.exit(f"Required tool '{tool}' not found in PATH.")

    name = ask_name()
    release = ask_release()
    disk_gib = ask_int("Root disk size in GiB", 20, 5, 4096)
    vcpus = ask_int("Number of vCPUs", 2, 1, 128)
    mem_gib = ask_int("Memory in GiB", 4, 1, 1024)
    data_disk = ask_data_disk()
    console = ask_console()

    version, os_variant = RELEASES[release]
    print(
        f"\nCreating VM '{name}': Ubuntu {version} ({release}), {vcpus} vCPU, "
        f"{mem_gib} GiB RAM, {disk_gib} GiB disk"
    )
    if input("Proceed? [y/N]: ").strip().lower() not in ("y", "yes"):
        sys.exit("Aborted.")

    image = ensure_base_image(release)

    vm_dir = IMAGE_DIR / name
    run(sudo(["mkdir", "-p", str(vm_dir)]))
    run(sudo(["chown", f"{os.getlogin()}:", str(vm_dir)]))

    disk = vm_dir / f"{name}.img"
    run([
        "qemu-img", "create",
        "-b", str(image), "-F", "qcow2",
        "-f", "qcow2", str(disk), f"{disk_gib}G",
    ])

    data_disk_path = None
    if data_disk:
        data_disk_path, data_disk_gib = data_disk
        if data_disk_path is None:
            data_disk_path = vm_dir / f"{name}-data.qcow2"
        run(sudo(["mkdir", "-p", str(data_disk_path.parent)]))
        run([
            "qemu-img", "create",
            "-f", "qcow2", str(data_disk_path), f"{data_disk_gib}G",
        ])

    pubkey = ssh_public_key()
    if not pubkey:
        print("Warning: no SSH public key found in ~/.ssh; VM will have no login key.")
    write_cloud_init(vm_dir, name, pubkey)

    disk_args = [f"--disk=path={disk},format=qcow2,bus=scsi"]
    if data_disk_path:
        disk_args.append(f"--disk=path={data_disk_path},format=qcow2,bus=scsi")

    run([
        "virt-install",
        f"--name={name}",
        f"--ram={mem_gib * 1024}",
        f"--vcpus={vcpus}",
        "--import",
        *disk_args,
        "--controller=type=scsi,model=virtio-scsi",
        f"--os-variant={os_variant}",
        "--network=bridge=virbr0,model=virtio",
        "--noautoconsole",
        "--cloud-init",
        f"user-data={vm_dir}/user-data.yaml,meta-data={vm_dir}/meta-data.yaml",
    ])

    ip = wait_for_ip(name)
    if ip:
        print(f"\nVM '{name}' is up at {ip} (ssh ubuntu@{ip})")
    else:
        print(f"\nNo IP address detected yet for '{name}'.")

    print(f"\nVM '{name}' created. Useful commands:")
    print(f"  virsh domifaddr {name}")
    print(f"  virsh console {name}")
    print(f"\nTo cleanup the VM '{name}'")
    print(f"  virsh shutdown {name}")
    print(f"  virsh undefine {name} --remove-all-storage")
    print(f"  rm -rf /var/lib/libvirt/images/{name}")

    if ip:
        launch_console(console, ip)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nAborted.")
    except subprocess.CalledProcessError as exc:
        sys.exit(f"Command failed with exit code {exc.returncode}.")