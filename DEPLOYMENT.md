# Linux Router 部署与故障排查

本文说明 Linux Router 的手动部署、网络配置、服务管理和常见故障排查。全新设备优先使用安装器
（见 [README.zh-CN.md](README.zh-CN.md)；网络受限时用 `get.sh` 走镜像）。手动部署适合开发、
定制目录或需要逐步检查系统配置的场景。

下文使用以下示例路径：

```bash
INSTALL_DIR=/home/router-panel
DATA_DIR=/home/router-panel/data
```

如使用其他路径，必须同步修改环境变量、systemd unit 和后续命令。

> 上机前的环境检查（sudo、DVD 源、时间、DNS、切换与回滚）见
> [上机清单](docs/INSTALL-CHECKLIST.md)。

## 1. 系统要求

目标系统为 Debian 13（x86_64）最小化安装，使用 systemd 与 **systemd-networkd**。
本分支只支持有线组网，不安装 `iw`、`wpasupplicant`、`hostapd`、`iptables`，
也不再使用 NetworkManager 或 dhcpcd。

安装依赖：

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  python3 \
  python3-flask \
  gunicorn \
  dnsmasq \
  nftables \
  iproute2 \
  udev \
  ppp \
  curl \
  ca-certificates
```

组件用途：

- `python3-flask`、`gunicorn`：运行 Web 服务；
- `systemd-networkd`（systemd 自带）：WAN 的 DHCP/SLAAC、LAN 静态地址、IPv6 RA/RDNSS、DHCPv6 前缀委派；
- `ppp`：PPPoE 拨号（`pppd` + `rp-pppoe.so`），只有 PPPoE 模式才真正用到；
- `dnsmasq`：为 LAN 提供 DHCPv4 与 DNS（IPv6 由 networkd 负责）；
- `nftables`：NAT 与 forward 规则（内核侧 `nf_tables`/`nf_nat` 由 Debian 内核自带）；
- `iproute2`、`udev`：网络状态查询、CAKE 限速（`tc`）与硬件信息。

项目不要求目标设备安装 Git。安装器和手动部署都可以使用源码目录或 GitHub 源码压缩包。

## 2. 部署程序文件

将项目文件部署到目标目录，并确保包含以下内容：

```text
$INSTALL_DIR/app.py
$INSTALL_DIR/agent.py
$INSTALL_DIR/router_panel/
$INSTALL_DIR/templates/
$INSTALL_DIR/static/
$INSTALL_DIR/router-panel.service
$INSTALL_DIR/router-panel-agent.service
```

例如从当前源码目录复制：

```bash
sudo install -d -m 0755 "$INSTALL_DIR"
sudo cp -a ./. "$INSTALL_DIR/"
sudo chown -R root:root "$INSTALL_DIR"
```

不要把运行时账号、密钥或网络配置放入源码目录。运行数据应保存在 `$DATA_DIR`。

## 3. 创建运行账号和数据目录

Web 服务以普通用户运行，Agent 以 root 运行并使用 `router-panel` 组限制 Unix Socket 访问：

```bash
sudo groupadd --system router-panel
sudo useradd --system \
  --gid router-panel \
  --home-dir "$DATA_DIR" \
  --no-create-home \
  --shell /usr/sbin/nologin \
  router-panel

sudo install -d -o router-panel -g router-panel -m 0700 "$DATA_DIR"
```

如果用户或组已经存在，不要重复创建；确认数据目录最终归属为 `router-panel:router-panel`。

初始化应用数据并生成开发环境默认账号：

```bash
sudo env \
  LINUX_ROUTER_DATA_DIR="$DATA_DIR" \
  LINUX_ROUTER_INITIAL_PASSWORD='CHANGE_THIS_PASSWORD' \
  python3 -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); import app"

# 将 CHANGE_THIS_PASSWORD 替换为实际初始密码
sudo chown -R router-panel:router-panel "$DATA_DIR"
sudo chmod 0700 "$DATA_DIR"
sudo find "$DATA_DIR" -type f -exec chmod 0600 {} +
```

如果未设置 `LINUX_ROUTER_INITIAL_PASSWORD`，开发环境默认密码为 `password`。生产部署应显式设置初始密码并在首次登录后立即修改。

## 4. 配置 systemd-networkd

网络后端是 **systemd-networkd**（Debian 13 自带），PPPoE 由 `pppd` 建会话。
NetworkManager、dhcpcd、ifupdown 都不要再管理同一批网卡。

启用服务：

```bash
sudo systemctl enable --now systemd-networkd.service
sudo systemctl enable --now systemd-resolved.service
```

> `systemd-resolved` 负责把 WAN 侧 DHCP 下发的 DNS 写进 `/etc/resolv.conf`；
> dnsmasq 只绑定 LAN 地址，不会和它的 127.0.0.53 冲突。

配置文件由面板生成（`/etc/systemd/network/`）：

| 文件 | 内容 |
| --- | --- |
| `10-linux-router-wan.network` | WAN 的 DHCP/SLAAC（PPPoE 模式不生成） |
| `20-linux-router-lan.network` | LAN 静态 IPv4、IPv6 地址、RA/RDNSS、`DHCPPrefixDelegation` |
| `30-linux-router-ppp.network` | 只匹配 `ppp0`，`DHCP=ipv6` + `WithoutRA=solicit` |
| `/etc/ppp/peers/linux-router` | PPPoE 会话参数（`plugin rp-pppoe.so`、账号、MTU 1492） |
| `/etc/ppp/chap-secrets`、`pap-secrets` | 宽带账号口令，权限 0600 |

手工检查：

```bash
networkctl list
networkctl status <网卡> --no-pager
ip -br addr
```

如果这台机器之前用 ifupdown 管过网卡，`/etc/network/interfaces` 里的 `iface <网卡> inet dhcp`
必须先注释掉（面板应用配置时会自动注释并备份为 `interfaces.linux-router.bak`），
否则开机时 `ifup` 会先抢走网卡。

## 5. 配置 IPv4/IPv6 转发

双栈路由需要同时开启两个转发开关：

```bash
sudo tee /etc/sysctl.d/90-router-panel.conf >/dev/null <<'EOF'
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
EOF
sudo sysctl --system
sysctl -n net.ipv4.ip_forward net.ipv6.conf.all.forwarding
```

期望输出：

```text
1
1
```

## 6. 配置 nftables 与 dnsmasq

面板生成两个文件，安装器负责把目录引入主配置：

```bash
# nftables：/etc/nftables.conf 需包含
include "/etc/nftables.d/*.nft"

# dnsmasq：/etc/dnsmasq.conf 需包含
conf-dir=/etc/dnsmasq.d/,*.conf
```

随后启用服务：

```bash
sudo systemctl enable --now nftables.service dnsmasq.service
```

如果系统启用了 `systemd-resolved`，它和 dnsmasq 会争用 53 端口，需要先停用：

```bash
sudo systemctl disable --now systemd-resolved.service
```

`/etc/nftables.d/90-linux-router.nft` 与 `/etc/dnsmasq.d/linux-router.conf` 的最终内容由面板的“路由”页面生成，手动部署时可以先用安装器写入的占位文件。

## 7. 安装并启动 systemd 服务

服务模板包含 `@INSTALL_DIR@` 和 `@DATA_DIR@` 占位符。生成实际 unit：

```bash
sudo sed \
  -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
  -e "s|@DATA_DIR@|$DATA_DIR|g" \
  "$INSTALL_DIR/router-panel-agent.service" \
  | sudo tee /etc/systemd/system/router-panel-agent.service >/dev/null

sudo sed \
  -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
  -e "s|@DATA_DIR@|$DATA_DIR|g" \
  "$INSTALL_DIR/router-panel.service" \
  | sudo tee /etc/systemd/system/router-panel.service >/dev/null

sudo chmod 0644 \
  /etc/systemd/system/router-panel-agent.service \
  /etc/systemd/system/router-panel.service

sudo systemd-analyze verify \
  /etc/systemd/system/router-panel-agent.service \
  /etc/systemd/system/router-panel.service

sudo systemctl daemon-reload
sudo systemctl enable --now router-panel-agent.service router-panel.service
```

检查服务：

```bash
sudo systemctl status router-panel-agent.service --no-pager
sudo systemctl status router-panel.service --no-pager
curl -sS http://127.0.0.1/healthz
```

健康检查应返回：

```json
{"status":"ok"}
```

Web 服务默认监听 TCP `80` 端口。服务需要绑定低端口能力，unit 中已配置 `CAP_NET_BIND_SERVICE`。

## 8. 首次登录和运行数据

安装或初始化完成后：

- 用户名：`admin`；
- 初始密码：由 `LINUX_ROUTER_INITIAL_PASSWORD` 指定，或使用开发环境默认值；
- 密码提示文件：`$DATA_DIR/initial_password.txt`。

以下文件属于运行数据，不应提交到 Git：

```text
$DATA_DIR/auth.json
$DATA_DIR/secret_key
$DATA_DIR/router.json
```

## 9. 路由、NAT 与双栈检查

WAN/LAN 角色、LAN 网段、DHCP 与 IPv6 参数保存在 `$DATA_DIR/router.json`，由面板“路由”页面写入。应用配置时面板会：

1. 写 `/etc/dnsmasq.d/linux-router.conf` 并 `dnsmasq --test` 校验；
2. 创建或修改 NetworkManager 连接 `linux-router-lan`（静态地址 + `never-default`）；
3. 写 `/etc/nftables.d/90-linux-router.nft`，用 `nft -c -f` 校验后 `nft -f` 加载；
4. 写 `/etc/sysctl.d/90-router-panel.conf` 并应用 IPv4/IPv6 转发；
5. 启用并重启 `dnsmasq.service`，必要时把 WAN 连接的 `ipv6.method` 改为 `auto`。

检查命令：

```bash
nmcli connection show linux-router-lan
ip -br addr show <LAN接口>
ip -6 route show default

nft list chain inet lr_forward forward
nft list chain ip lr_nat postrouting
nft list chain ip6 lr_nat6 postrouting

sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding
cat /var/lib/misc/dnsmasq.leases
```

IPv6 模式说明：

- `运营商 PD 自动`：`linux-router-pd.service` 里的 dhcpcd 在 WAN 上请求前缀，把第一个 /64 配到 LAN，hook 重写 dnsmasq；
- `ULA + NAT66`：LAN 用 `fdxx::/64`，nftables 生成 `lr_nat6` 做 masquerade，不依赖运营商；
- `全局前缀`：把运营商分配或自行委派得到的 /64 填进去，不做 NAT66，只做转发；
- `关闭`：dnsmasq 不发 RA，`lr_nat6` 表会被删除。

### PD 模式说明

前缀委派由 systemd-networkd 的 `DHCPPrefixDelegation=` 完成（systemd ≥ 250），不再有
`linux-router-pd.service` 或 dhcpcd hook：

- LAN 的 `.network` 里 `DHCPPrefixDelegation=yes` + `IPv6SendRA=yes`
- `ppp0` 的 `.network` 里 `[DHCPv6] WithoutRA=solicit`（PPP 链路上没有 RA）
- 前缀变化由 networkd 自己处理，没有 hook、不需要重启服务

```bash
networkctl status <LAN> --no-pager | sed -n '/Address/,+6p'
ip -6 addr show dev <LAN> scope global
```

### 网络栈归属

一台机器上可能同时存在 systemd-networkd、NetworkManager、dhcpcd、ifupdown，先看清谁在管哪块网卡：

| 组件 | 判断方法 |
| --- | --- |
| systemd-networkd | `networkctl status <网卡>` 里有 `Network File: /etc/systemd/network/...` |
| NetworkManager | `nmcli device status` 里出现该网卡，且 `systemctl is-active NetworkManager` |
| dhcpcd | `ls /run/dhcpcd/*.pid`（每接口实例）与 `/run/dhcpcd/pid` |
| ifupdown | `/etc/network/interfaces` 里有 `iface <网卡> inet dhcp`；`systemctl status ifup@<网卡>` |

`dhcpcd.service` 处于 inactive 并不代表没有 dhcpcd 在跑：ifupdown 启动时会拉起一个
只针对单块网卡的实例（`/run/dhcpcd/<网卡>.pid`）。

面板的「依赖」页有「遗留网络栈」与「WAN 接管情况」两项检查；点「切换到 networkd」会：

1. 写入 `/etc/systemd/network/*.network`
2. 停掉 `NetworkManager.service`、`dhcpcd.service` 和对应的 `ifup@<网卡>`
3. 注释 `/etc/network/interfaces` 中这些网口（备份 `.linux-router.bak`）
4. 重启 `systemd-networkd` 并 `networkctl reconfigure`

## 10. 常见故障排查

### Web 页面无法打开

```bash
sudo systemctl status router-panel.service --no-pager
sudo journalctl -u router-panel.service -n 100 --no-pager
sudo ss -ltnp | grep ':80 '
curl -v http://127.0.0.1/healthz
```

### Agent 不可用

```bash
sudo systemctl status router-panel-agent.service --no-pager
sudo journalctl -u router-panel-agent.service -n 100 --no-pager
sudo ls -l /run/linux-router/agent.sock
```

确认 Web 服务和 Agent 使用相同的 `LINUX_ROUTER_DATA_DIR`、Socket 路径以及 `router-panel` 组。

### 网卡显示为 unmanaged

```bash
nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status
grep -R "^[[:space:]]*renderer:" /etc/netplan 2>/dev/null
sudo udevadm info -q property -p /sys/class/net/<接口名> | grep NM_UNMANAGED
```

确认 NetworkManager 使用 `managed=true`，netplan renderer 为 `NetworkManager`，并且没有其他服务接管同一接口。修改后在维护窗口执行 `netplan generate`、`netplan apply` 和 NetworkManager 重启。

### LAN 设备拿不到地址

```bash
systemctl status dnsmasq.service --no-pager
journalctl -u dnsmasq -n 50 --no-pager
ss -lunp | grep ':53'
cat /etc/dnsmasq.d/linux-router.conf
```

确认 `interface=` 是 LAN 网口、`dhcp-range` 在 LAN 网段内、53 端口没有被 `systemd-resolved` 占用。修改配置后在面板“路由”页面重新应用。

### 有地址但上不了网

依次确认：

1. WAN 接口有默认路由（`ip route`、`ip -6 route`）；
2. `net.ipv4.ip_forward` 为 `1`；
3. `nft list chain ip lr_nat postrouting` 里有 masquerade 规则；
4. LAN 网卡地址与 DHCP 网关一致。

```bash
ip route; ip -6 route
sysctl net.ipv4.ip_forward net.ipv6.conf.all.forwarding
nft list ruleset | sed -n '/lr_nat/,/}/p'
nmcli -g ipv4.addresses,ipv4.never-default connection show linux-router-lan
```

如果面板“依赖”页提示 NAT 规则未加载，点“重新加载规则”即可（等价于 `nft -f /etc/nftables.d/90-linux-router.nft`）。

### 被访问控制锁在外面（面板和 SSH 都连不上）

「保护」模式会丢弃所有来自 WAN 的新入站连接。如果你从 WAN 侧访问面板、又没把面板端口
（和 SSH 的 22）填进「WAN 放行 TCP 端口」，应用之后就会立刻失联。已经建立的连接不受影响，
所以正在进行的 SSH 会话可能还活着。

在设备本地控制台（或 iKuai/Proxmox 的 VM 控制台）执行，立即恢复访问：

```bash
nft delete table inet lr_input
```

这条只删运行时的表，`/etc/nftables.d/90-linux-router.nft` 里仍是保护模式，**重载或重启会再次生效**。
所以恢复访问后立刻用浏览器进面板，把「访问控制」改回「开放」，或保持「保护」但把
面板端口与 22 填进「WAN 放行 TCP 端口」，再应用一次。

不想开浏览器时，也可以用面板自己的代码重写规则（改配置 + 立即加载，一次到位）：

```bash
cd /opt/linux-router && LINUX_ROUTER_DATA_DIR=/var/lib/linux-router python3 -c "
import sys; sys.path.insert(0, '/opt/linux-router')
from router_panel import routing, firewall
cfg = routing.load_router_config()
cfg['firewall_mode'] = 'open'
routing.save_router_config(cfg)
print(firewall.apply_ruleset(firewall.render_nftables_ruleset(cfg)).output)
"
```

面板本身会拦下这种操作：从 LAN 以外的地址提交「保护」、而面板端口又不在放行列表时，
表单会直接报错并提示先填端口，不会真的应用。

### PD 拿不到前缀

```bash
networkctl status <WAN> --no-pager | sed -n '/Delegated/,+4p'
networkctl status <LAN> --no-pager | sed -n '/Address/,+6p'
journalctl -u systemd-networkd -n 80 --no-pager
sudo tcpdump -i <WAN> -nv 'ip6 and (port 546 or port 547)'
```

依次确认：

1. WAN 选的是逻辑接口（PPPoE 是 `ppp0`，不是物理网卡），且 `30-linux-router-ppp.network` 已生成；
2. 抓包里 Advertise 是否带 `IA_PD`（不带就是运营商没给前缀，只能用 ULA + NAT66）；
3. 光猫是否真的桥接（路由模式下前缀会被光猫吃掉）；
4. `networkctl status <LAN>` 里没有 `2408:...` 说明 LAN 的 `.network` 没拿到前缀，
   看 `journalctl -u systemd-networkd` 里的 DHCPv6 日志。

### IPv6 不通

```bash
ip -6 addr show <LAN接口>
ip -6 route show default
sysctl net.ipv6.conf.all.forwarding
nft list chain ip6 lr_nat6 postrouting   # 仅 ULA 模式存在
rdisc6 <LAN接口>                          # 可选，查看 RA
```

- ULA 模式：LAN 必须有 `fdxx::1/64`，且 `lr_nat6` 表存在；
- 全局前缀模式：WAN 必须拿到公网 IPv6 且默认路由存在，LAN 前缀必须与运营商分配一致；
- 客户端只拿到 `fe80::` 说明 RA 没有发出，检查 `enable-ra` 与 LAN 接口地址。

### 修改配置后未生效

```bash
sudo systemctl restart router-panel.service
sudo systemctl restart router-panel-agent.service
```

修改 `static/style.css` 后，还需递增 `templates/base.html` 中 CSS URL 的 `v` 参数。

## 11. 卸载和恢复

如果程序由安装器部署，推荐使用安装器卸载：

```bash
sudo bash install.sh uninstall \
  --install-dir /home/router-panel \
  --data-dir /home/router-panel/data
```

默认卸载保留账号、密钥和路由配置，同时清理：

- `router-panel` / `router-panel-agent` 及 `linux-router-cake|dns|pppoe` 的 unit
- `/etc/systemd/network/{10,20,30}-linux-router-*.network`
- `/etc/ppp/peers/linux-router`、`/etc/ppp/ip-up.d/90-linux-router`、`chap-secrets`、`pap-secrets`
- `/etc/linux-router`、`/run/linux-router`
- `nftables.d/90-linux-router.nft`、`dnsmasq.d/linux-router.conf` 与四张 `lr_*` nft 表

并从安装时记录的状态恢复 `nftables.conf`、`dnsmasq.conf`、sysctl、IPv4/IPv6 转发以及
`dhcpcd`、`systemd-resolved` 的启停状态；如果面板曾注释过 `/etc/network/interfaces`，
会从 `interfaces.linux-router.bak` 还原。

使用 `--purge-data` 会删除全部运行数据，且不可恢复。

如果安装或升级在健康检查前失败，安装器会尝试恢复应用目录、数据目录、项目网络配置和服务状态。
恢复后仍应检查 `systemctl` 与默认路由。

## 12. 开发验证

开发环境可分别启动 Agent 和 Web：

```bash
cd "$INSTALL_DIR"
sudo env \
  LINUX_ROUTER_DATA_DIR="$DATA_DIR" \
  LINUX_ROUTER_AGENT_SOCKET=/run/linux-router/agent.sock \
  python3 agent.py

# 另一个终端
python3 app.py
```

运行测试：

```bash
python3 -m unittest tests.test_application
```

修改 Web 代码或模板后重启 `router-panel.service`；修改 Agent、系统查询或网络操作后重启 `router-panel-agent.service`。生产环境应使用 systemd 管理的 Gunicorn 服务。
