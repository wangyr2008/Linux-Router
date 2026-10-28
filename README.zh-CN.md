[English](README.md) | [简体中文](README.zh-CN.md)

<h1>
  <img src="static/favicon.ico" alt="" width="32" height="32" align="center">
  Linux Router（Debian 13 · 有线双栈 · nftables 版）
</h1>

把一台 x86 工控机变成纯有线双栈路由器：WAN 接光猫/上级路由，LAN 接交换机或终端，Web 控制台负责看状态、配 LAN、管 DHCP 和 IPv6。

本分支基于 [Jaksay/Linux-Router](https://github.com/Jaksay/Linux-Router) 改造，面向 **Debian 13 最小化安装（debian-13.x.x-amd64-netinst）** 的 x86 设备，只使用系统自带或一个 `apt install` 就能装上的组件。

![Linux Router Web 管理界面](docs/assets/linux-router-web-console.png)

## 与上游的差异

| 项目 | 上游 | 本分支 |
| --- | --- | --- |
| 无线 | Wi-Fi 连接、热点、AP+STA 并发、热点保活 | **全部移除**，只保留有线 |
| 转发后端 | `iptables` + NetworkManager `ipv4.method shared` | **nftables**（Debian 13 默认），规则由面板生成 |
| IPv6 | 无 | **双栈**：WAN 自动 SLAAC/DHCPv6，LAN 支持 ULA+NAT66 或全局前缀 |
| DHCP/DNS | dnsmasq-base（仅热点用） | `dnsmasq` 独立服务，提供 DHCPv4、RA、DHCPv6 与 LAN DNS |
| 依赖 | `iptables`、`iw`、`wpasupplicant`、`hostapd` 相关 | 去掉以上全部，仅保留 Flask/Gunicorn/NetworkManager/dnsmasq/nftables |
| 页面 | 概览 / 依赖 / 有线 / 无线 / 热点 / 工具 / 设备 / 设置 | 概览 / 依赖 / 有线 / **路由** / 设备 / 工具 / 设置 |

## 功能

- **系统概览**：主机、CPU、内存、磁盘、活动连接（含 IPv4/IPv6 地址）、网卡硬件信息
- **路由页**：选择 WAN/LAN 网口，设置 LAN IPv4 网段、DHCP 地址池与租期、上游 DNS、MTU、IPv6 模式与前缀，一键应用
- **有线页**：每块网卡的链路状态、协商速率、接口角色、IPv4/IPv6 地址、网关、DNS、配置方式
- **设备页**：dnsmasq 租约 + 邻居表合并展示，能看到 IPv4、IPv6、MAC、租约剩余时间、在线状态
- **依赖页**：组件、服务、网卡接管、转发与 NAT 规则的分项体检，并提供一键修复动作
- **工具页**：Tailscale 登录辅助、服务监控、重启等

## 系统要求

- x86_64 工控机，至少两块有线网卡（WAN / LAN）
- Debian 13（trixie）最小化安装 + systemd + apt
- NetworkManager 管理网卡（安装器会启用）

安装器只会安装这些包，其余全部使用 Debian 自带组件：

```text
python3 python3-flask gunicorn network-manager dnsmasq nftables iproute2 udev curl tar ca-certificates
```

> 内核侧的 `nf_tables`、`nf_nat`、`nft_chain_nat` 是 Debian 内核自带模块，不需要额外安装或编译。

## 安装

方式一：从本机源码目录安装（把仓库拷到设备上，最省事）：

```bash
# 在设备上，源码目录例如 /root/Linux-Router
sudo bash /root/Linux-Router/install.sh install --source-dir /root/Linux-Router
```

方式二：从 GitHub 分支安装（先把本分支推到你自己的仓库/分支）：

```bash
curl -fsSL https://raw.githubusercontent.com/<你的账号>/Linux-Router/<分支>/install.sh \
  -o /tmp/linux-router-install.sh
sudo bash /tmp/linux-router-install.sh install
```

也可以用 `--archive-url https://.../source.tar.gz` 指定任意 HTTPS 源码包。

安装器会：

1. 安装上面列出的软件包；
2. 部署程序到 `/opt/linux-router`，数据放在 `/var/lib/linux-router`；
3. 创建 `router-panel` 服务账号并生成初始密码；
4. 写入 `net.ipv4.ip_forward=1` 与 `net.ipv6.conf.all.forwarding=1`；
5. 准备 `/etc/nftables.d/90-linux-router.nft` 与 `/etc/dnsmasq.d/linux-router.conf` 的占位文件，并确保
   `/etc/nftables.conf`、`/etc/dnsmasq.conf` 引入了对应目录；
6. 启用 `nftables.service`、`dnsmasq.service`、NetworkManager 和两个面板服务；
7. 如检测到 `systemd-resolved` 占用 53 端口，会停用并禁用（卸载时恢复）。

默认位置：

- 程序：`/opt/linux-router`
- 数据：`/var/lib/linux-router`
- Web 服务：`router-panel.service`（Gunicorn，80 端口）
- root Agent：`router-panel-agent.service`（Unix Socket `/run/linux-router/agent.sock`）

安装完成后打开 `http://<LAN口IP或临时IP>/`，用 `admin` 和安装结束输出的密码登录。

## 首次配置：路由页

安装器不会猜你的网口用途，进面板后到 **路由** 页面完成一次性配置：

1. **WAN 上行接口**：接光猫/上级路由的网口（IPv4 由 NetworkManager 的该连接决定，DHCP 或静态都行）
2. **LAN 局域网接口**：接交换机的网口，面板会为它创建 `linux-router-lan` 连接，地址固定为网段第一个可用地址
3. **LAN IPv4 网段**：默认 `192.168.50.0/24`
4. **DHCP 地址池 / 租期**：留空自动取网段后半段
5. **IPv6 模式**：见下一节
6. 点击 **应用路由配置**

应用时会依次写入 dnsmasq 配置 → 配置 LAN 地址 → 加载 nftables 规则 → 启用转发 → 重启 dnsmasq。
**如果改了 LAN 地址，当前页面需要用新地址重新打开。**

## IPv6 三种模式

| 模式 | 适用场景 | LAN 前缀 | NAT66 | 说明 |
| --- | --- | --- | --- | --- |
| `ULA + NAT66`（默认） | 运营商没给 IPv6，或不想折腾前缀委派 | 自动生成 `fdxx:xxxx:xxxx::/64` | 开 | 内网设备可访问 IPv6 外网，外部无法主动连入 |
| `全局前缀（纯路由）` | 运营商已下发固定 /64，或你自己做了前缀委派 | 你填写的 `2408:...::/64` | 关 | 标准路由，内网设备拿到公网 IPv6 |
| `关闭` | 只要 IPv4 | 无 | 关 | dnsmasq 不发 RA |

WAN 侧默认勾选“WAN 自动获取 IPv6”，面板会把 WAN 连接的 `ipv6.method` 设为 `auto`（SLAAC + DHCPv6）。

> **关于前缀委派（PD）**：NetworkManager 本身不做 PD。如果你的运营商只给 PD 不给固定 /64，可以在系统里另外跑
> `dhcpcd` 或 `odhcp6c` 拿到前缀，然后把分到的 /64 填到“全局前缀”模式里，本面板不做 NAT66，只做转发。
> 想完全零依赖，用默认的 ULA + NAT66 即可。

## nftables 规则

面板生成 `/etc/nftables.d/90-linux-router.nft`，由 `/etc/nftables.conf` 的
`include "/etc/nftables.d/*.nft"` 引入，开机由 `nftables.service` 加载。内容只做两件事：

```nft
table inet lr_forward { ... }   # forward 链：放行 LAN↔WAN、LAN↔LAN、IPv6 ND/PMTU
table ip   lr_nat    { ... }    # IPv4 masquerade（带 counter，面板可读匹配包数）
table ip6  lr_nat6   { ... }    # IPv6 masquerade，仅 ULA 模式生成
```

- **不动 `input` 链**，避免把自己锁在门外；需要防火墙策略请自行在 `nftables.conf` 里加表
- 表名固定为 `lr_forward` / `lr_nat` / `lr_nat6`，每次加载先 `delete` 再重建，可反复 `nft -f` 或 `systemctl reload nftables`
- 查看当前规则：`sudo nft list ruleset`
- 查看 NAT 计数：`sudo nft list chain ip lr_nat postrouting`

## 常用排查

```bash
# 服务
systemctl status router-panel router-panel-agent dnsmasq nftables --no-pager

# 地址与路由
ip -br addr
ip -6 route show default
nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status
nmcli connection show linux-router-lan

# 转发与规则
sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding
nft list ruleset
nft list chain ip lr_nat postrouting

# DHCP 租约
cat /var/lib/misc/dnsmasq.leases

# 日志
journalctl -u dnsmasq -n 50 --no-pager
journalctl -u router-panel-agent -n 50 --no-pager
```

常见问题：

- **LAN 设备拿不到地址**：`dnsmasq` 是否运行、`/etc/dnsmasq.d/linux-router.conf` 里的 `interface=` 是否是 LAN 网口、53 端口是否被 `systemd-resolved` 占用
- **能拿地址但不能上网**：`ip_forward` 是否为 1、`nft list chain ip lr_nat postrouting` 是否有 masquerade 规则、WAN 是否有默认路由
- **IPv6 不通**：确认 IPv6 转发为 1、`nft list chain ip6 lr_nat6 postrouting` 是否有规则（ULA 模式）、LAN 网卡是否拿到 `fdxx::1/64`

## 卸载

```bash
sudo bash /tmp/linux-router-install.sh uninstall            # 保留账号与路由配置
sudo bash /tmp/linux-router-install.sh uninstall --purge-data  # 一并删除
```

卸载会删除两个服务、`/etc/nftables.d/90-linux-router.nft`、`/etc/dnsmasq.d/linux-router.conf`，
恢复安装前的 `nftables.conf`、`dnsmasq.conf`、NetworkManager/netplan/sysctl 配置以及
`dhcpcd`、`systemd-resolved` 的启停状态。

## 开发与测试

```bash
# 终端一：root Agent
sudo env LINUX_ROUTER_DATA_DIR=/var/lib/linux-router \
  LINUX_ROUTER_AGENT_SOCKET=/run/linux-router/agent.sock python3 agent.py

# 终端二：Web
python3 app.py

# 测试（不需要 root，也不依赖 nmcli/nft/dnsmasq）
python3 -m unittest tests.test_application
```

改代码后：Web/模板改动重启 `router-panel.service`；Agent/路由逻辑改动重启 `router-panel-agent.service`；
改了 `static/style.css` 记得递增 `templates/base.html` 里的 CSS `v` 参数。

## 开源协议

GNU General Public License v3.0 or later，详见 [LICENSE](LICENSE)。本分支同样以 GPLv3 发布，修改版分发时请保留同样的授权。
