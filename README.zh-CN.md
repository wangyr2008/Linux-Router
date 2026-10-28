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
| IPv6 | 无 | **双栈**：WAN 自动 SLAAC/DHCPv6，LAN 支持 **运营商 PD 自动**、ULA+NAT66 或全局前缀 |
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

# 需要公网 IPv6（PD 模式）时顺便装上 dhcpcd
sudo bash /root/Linux-Router/install.sh install --source-dir /root/Linux-Router --with-pd
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

## IPv6 四种模式

| 模式 | 适用场景 | LAN 前缀 | NAT66 | 说明 |
| --- | --- | --- | --- | --- |
| `运营商 PD 自动` | 光猫桥接、工控机自己 PPPoE 拨号 | 运营商委派的前缀里取第一个 /64 | 关 | 内网设备拿到**公网 IPv6**，可被外部访问；前缀变化自动跟随 |
| `ULA + NAT66`（默认） | 运营商没给 IPv6，或光猫是路由模式 | 自动生成 `fdxx:xxxx:xxxx::/64` | 开 | 内网设备能出不能进 |
| `全局前缀（纯路由）` | 已知道固定 /64，或 PD 由别处代劳 | 你填写的 `2408:...::/64` | 关 | 标准路由，前缀变了要手改 |
| `关闭` | 只要 IPv4 | 无 | 关 | dnsmasq 不发 RA |

WAN 侧默认勾选“WAN 自动获取 IPv6”，面板会把 WAN 连接的 `ipv6.method` 设为 `auto`（SLAAC + DHCPv6）。
PPPoE 拨号时 WAN 要选 **ppp0**（逻辑接口），不是物理网卡。

### PD 模式怎么工作

NetworkManager 至今不支持 DHCPv6-PD，所以 PD 模式用一个只做前缀委派的 `dhcpcd` 实例补上这一环：

```text
WAN(ppp0) --DHCPv6-PD--> dhcpcd --ia_pd--> 拿到 2408:...:2f00::/60
                                            └─ 第一个 /64 配到 LAN(enp2s0)
                                               └─ hook 重写 dnsmasq 的 RA 前缀并重启
```

- 依赖只多一个 `dhcpcd`（Debian 13 自带仓库，约 300KB），安装时加 `--with-pd`，或在面板“依赖”页点“安装 PD 客户端”
- `dhcpcd` 用独立配置 `/etc/linux-router/dhcpcd.conf` 和独立服务 `linux-router-pd.service`，**不改** `/etc/dhcpcd.conf`
- 配置里 `noipv4` + `noipv6rs` + `nohook resolv.conf, hostname`：IPv4、WAN 地址、DNS 仍归 NetworkManager，dhcpcd 只负责要前缀
- LAN 的 IPv6 地址由 dhcpcd 分配，所以 NetworkManager 的 LAN 连接被设为 `ipv6.method ignore`，避免两边互相清地址
- **前缀变化**：运营商换前缀时 dhcpcd 触发 `DELEGATED6`，hook 调 `pd_hook.py`，重新算 LAN /64、重写 dnsmasq、重启服务，内网设备收到新 RA 后自动换地址
- 前缀丢了（`EXPIRE6`/`STOP6`）：清空 LAN 前缀并停发 RA，不会残留旧地址

#### 为什么这里的 dhcpcd 不会和 NetworkManager 打架

会打架的是**让 dhcpcd 当完整网络管理器**：它默认会抢 IPv4 地址、默认路由、`/etc/resolv.conf` 和 hostname，和 NetworkManager 两套状态互相覆盖。我们把它限制成"只做一件事"：

| 手段 | 作用 |
| --- | --- |
| `noipv4` + 启动参数 `-6` | 完全不碰 IPv4，WAN 的 IPv4 地址仍由 NetworkManager 提供 |
| `noipv6rs` + `nohook resolv.conf, hostname` | 不发/不收 RA，不改 DNS 和主机名（这也是 dhcpcd 手册对 `ia_pd` 的明确建议） |
| 独立配置 + 独立 unit | 不读也不改 `/etc/dhcpcd.conf`，不占用发行版的 `dhcpcd.service` |
| 命令行显式列出 `WAN LAN` | 只在指定网口上跑，`ia_pd` 要求"dhcpcd 必须在被委派的接口上运行" |
| LAN 连接 `ipv6.method ignore` | LAN 的 IPv6 地址只由 dhcpcd 管，NetworkManager 不去清 |

反过来，如果启动前发现别的 dhcpcd 实例已经占着 WAN/LAN，面板**拒绝启动 PD 并说明原因**，而不是把对方的地址抢掉。

#### WAN 被 ifupdown 管着怎么办

Debian netinst 安装时会写 `/etc/network/interfaces`：

```text
allow-hotplug ens3
iface ens3 inet dhcp
```

而 NetworkManager 默认 `[ifupdown] managed=false`，**不接管这类网口**——它们由 ifupdown 启动的一个 dhcpcd 实例在管（`/run/dhcpcd/ens3.pid`，注意此时 `dhcpcd.service` 是 inactive，只看服务状态会漏掉）。后果是面板对 WAN 的设置（自动获取 IPv6、MTU、静态地址）写了也不生效，PD 模式也会被拒绝启动。

路由页会在这种情况下直接给出提示，依赖页的「WAN 接口归属」也会标黄。移交办法：

```bash
sudo cp /etc/network/interfaces /etc/network/interfaces.bak
sudo sed -i 's/^allow-hotplug ens3/# allow-hotplug ens3/; s/^iface ens3 inet dhcp/# iface ens3 inet dhcp/' /etc/network/interfaces
sudo systemctl stop ifup@ens3.service     # 杀掉那个 dhcpcd 实例
sudo systemctl restart NetworkManager     # NM 接管并重新 DHCP
```

> ⚠️ 这条命令会让该网口短暂断网。如果这台机器是你 SSH 的目标，从 WAN 侧操作会掉线；请从 LAN 侧或本地控制台执行。

验证：

```bash
systemctl status linux-router-pd --no-pager
dhcpcd --variables | grep -i prefix     # 或在 WAN 上抓包看 IA_PD
ip -6 addr show dev <LAN> scope global  # 应该有 2408:...::1/64
cat /var/lib/linux-router/pd.json        # 面板显示的委派前缀就来自这里
journalctl -u linux-router-pd -n 50 --no-pager
```

> 前缀委派要求运营商真的给 PD（多数家宽给 /60 或 /56，桥接 + 自己拨号才拿得到）。
> 如果光猫是路由模式，前缀会被光猫吃掉，这时只能用 ULA + NAT66。

## CAKE 限速（按带宽整形）

设置页可开启 CAKE，按 Mbit/s 填运营商带宽即可，用于压制缓冲膨胀（bufferbloat）：

| 方向 | 实现 | 说明 |
| --- | --- | --- |
| 上行 | WAN 出方向 `tc qdisc ... root cake bandwidth <n>mbit` | 必填 |
| 下行 | 专用 `ifb-lr` 设备镜像 WAN 入方向后再整形 | 可留空，留空则只处理上行 |

- 生效方式：写入 `/etc/linux-router/cake.sh` + `linux-router-cake.service`，开机自动加载
- 自愈：`linux-router-cake.timer` 每 5 分钟跑一次 `ensure`，PPPoE 重连导致设备重建后会自动补挂；
  `ensure` 是幂等的（队列已存在就不动），不会重置统计
- 填带宽建议为标称值的 85%~95%，填满反而容易排队
- 修改 WAN 接口后无需单独操作，应用路由配置时会同步刷新限速

手工核对：

```bash
tc qdisc show dev <WAN>        # 期望 qdisc cake ... bandwidth <上行>Mbit
tc qdisc show dev ifb-lr       # 有下行时才有，bandwidth <下行>Mbit
systemctl status linux-router-cake --no-pager
```

> 想临时关掉：`sh /etc/linux-router/cake.sh clear`，或在设置页取消勾选后应用。

## 访问控制（入站防护）

默认是「开放」：只做 NAT，不拦截任何入站连接。**在 PD / 全局前缀模式下，LAN 设备持有公网 IPv6，
这时必须切到「保护」**，否则内网设备等于直接挂在公网上。

「保护」模式会额外生成 `inet lr_input`（`policy drop`），并把 forward 链改成 `policy drop`：

| 放行项 | 用途 |
| --- | --- |
| 已建立/相关连接、ICMP、ICMPv6 | 必须，PMTU 与邻居发现依赖它 |
| `lo` | 本机回环（健康检查走这里） |
| WAN `udp 68/546` | DHCPv4 / DHCPv6 客户端应答 |
| LAN `tcp 22/53/80` + 自定义 | SSH、DNS、面板 |
| LAN `udp 53/67/547` | DNS、DHCPv4 服务、DHCPv6 服务 |
| `tailscale0` | Tailscale 远程管理与子网路由 |
| WAN 自定义 TCP/UDP | 需要从外网直连本机时才填 |

forward 链在保护模式下只放行 `iifname <LAN>` 与 `tailscale0`：LAN 可以出去，外部不能主动进来。

> ⚠️ 如果你从 WAN 侧 SSH 管理这台机器，**先把 22 填进「WAN 放行 TCP 端口」**再切保护模式，否则会把自己关在门外。
> ⚠️ 目前没有做端口转发（DNAT）。要让外网访问内网某台机器，需要手写 dnat 规则——需要的话可以让我加进面板。

## 与 sing-box / 透明代理共存

能用。这套规则与 sing-box 的 TUN / `auto_redirect` 不冲突（sing-box 只动自己创建的表），
但下面四件事不处理，会出现“看着通了其实绕过了代理”：

1. **路由回环**：TUN 的 `auto_route` 会把默认路由指到 tun0，路由器自己的出站也会被卷进隧道。
   给 outbound 加 `bind_interface: "ppp0"`（或 `inet4_bind_address`），让出站从 WAN 直接走。
2. **IPv6 泄漏**：PD 模式下 LAN 有公网 IPv6，tun 只配 `inet4_address` 的话 IPv6 会绕过代理直连。
   要全量代理就给 tun 加 `inet6_address` + `auto_route`；只代理 IPv4 的话，至少把访问控制留在「保护」。
3. **DNS 归属**：LAN 设备的 DNS 是本机 dnsmasq。想让 sing-box 的分流规则生效，把面板「上游 DNS」填
   `127.0.0.1#5353`（sing-box 的 dns listen 配 `127.0.0.1:5353`）。填了之后面板会写 `no-resolv`，
   dnsmasq 不再叠加 `/etc/resolv.conf`，避免查询漏到运营商 DNS。
4. **不要 `nft flush ruleset`**：sing-box 自己的规则没问题，但启动脚本里若有 `flush ruleset`，
   会把 `lr_forward`/`lr_nat`/`lr_nat6`/`lr_input` 一起清掉，NAT 和防护立刻失效。
   改完规则用 `systemctl reload nftables`。

另外 sing-box 的 Clash API（默认 9090）属于 input 服务，保护模式下要访问就把它填进「LAN 放行 TCP 端口」。

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
