# Linux Router 部署与故障排查

本文说明 Linux Router 的手动部署、网络配置、服务管理和常见故障排查。全新设备优先使用 [README.zh-CN.md](README.zh-CN.md) 中的安装器；手动部署适合开发、定制目录或需要逐步检查系统配置的场景。

下文使用以下示例路径：

```bash
INSTALL_DIR=/home/router-panel
DATA_DIR=/home/router-panel/data
```

如使用其他路径，必须同步修改环境变量、systemd unit 和后续命令。

## 1. 系统要求

目标系统为 Debian 13（x86_64）最小化安装，并使用 systemd、apt 和 NetworkManager。本分支只支持有线组网，不安装 `iw`、`wpasupplicant`、`hostapd` 或 `iptables`。

安装依赖：

```bash
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  python3 \
  python3-flask \
  gunicorn \
  network-manager \
  dnsmasq \
  nftables \
  iproute2 \
  udev \
  curl \
  ca-certificates
```

需要「运营商 PD 自动」模式时再补一个包（其余模式不需要）：

```bash
sudo apt-get install -y --no-install-recommends dhcpcd
```

组件用途：

- `python3-flask`、`gunicorn`：运行 Web 服务；
- `network-manager`：管理有线接口、WAN 的 DHCP 客户端与 LAN 的静态地址；
- `dnsmasq`：为 LAN 提供 DHCPv4、RA、DHCPv6 与 DNS；
- `nftables`：提供 NAT 与 forward 规则（内核侧 `nf_tables`/`nf_nat` 由 Debian 内核自带）；
- `iproute2`、`udev`：系统网络和硬件信息查询。

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

## 4. 配置 NetworkManager 和 netplan

启用 NetworkManager：

```bash
sudo systemctl enable --now NetworkManager.service
```

确认传统接口由 NetworkManager 管理。`/etc/NetworkManager/NetworkManager.conf` 至少应包含：

```ini
[ifupdown]
managed=true
```

如果系统使用 netplan，应将 renderer 设置为 `NetworkManager`。建议先备份现有配置：

```bash
sudo cp -a /etc/netplan "/etc/netplan.backup.$(date +%Y%m%d%H%M%S)"
```

项目生成的最小配置示例：

```yaml
network:
  version: 2
  renderer: NetworkManager
```

保存后，在本地控制台或维护窗口执行：

```bash
sudo netplan generate
sudo netplan apply
sudo systemctl restart NetworkManager.service
```

检查 renderer 和网卡状态：

```bash
grep -R "^[[:space:]]*renderer:" /etc/netplan 2>/dev/null
nmcli -t -f DEVICE,TYPE,STATE,CONNECTION device status
```

如果系统使用 `dhcpcd` 管理同一批接口，应在维护窗口确认后停止并禁用：

```bash
sudo systemctl disable --now dhcpcd.service
```

不要让 `dhcpcd`、`systemd-networkd` 和 NetworkManager 同时管理同一接口。

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

### PD 模式相关文件

| 路径 | 作用 |
| --- | --- |
| `/etc/linux-router/dhcpcd.conf` | 只做 PD 的 dhcpcd 配置（`noipv4`、`noipv6rs`、`ia_pd`） |
| `/usr/lib/dhcpcd/dhcpcd-hooks/99-linux-router` | dhcpcd 事件 hook，被 `dhcpcd-run-hooks` source |
| `/etc/systemd/system/linux-router-pd.service` | 独立 dhcpcd 实例，不占用发行版 `dhcpcd.service` |
| `$DATA_DIR/pd.json` | 当前委派前缀、触发事件、上次同步时间与错误 |
| `/opt/linux-router/pd_hook.py` | hook 的入口，重算 LAN /64 并重写 dnsmasq |

PD 模式会把 LAN 的 NetworkManager 连接设为 `ipv6.method ignore`，让 dhcpcd 独占 IPv6 地址；IPv4 仍由 NetworkManager 管理。

### 网络栈归属（ifupdown / NetworkManager / dhcpcd）

一台 Debian 上可能同时存在三套东西，先看清谁在管哪块网卡，否则会出现"面板改了没反应"：

| 组件 | 判断方法 |
| --- | --- |
| ifupdown | `/etc/network/interfaces` 里有 `iface <网卡> inet dhcp`；`systemctl status ifup@<网卡>` |
| NetworkManager | `nmcli -t -f DEVICE,STATE,CONNECTION device status`，连接名不是 `Ifupdown (xxx)` |
| dhcpcd | `ls /run/dhcpcd/*.pid`（每接口实例）与 `/run/dhcpcd/pid`（管理器实例） |

关键点：**`dhcpcd.service` 处于 inactive 并不代表没有 dhcpcd 在跑**。Debian netinst 会写
`iface ens3 inet dhcp`，ifupdown 启动时就会拉起一个只针对 ens3 的 dhcpcd 实例
（`/run/dhcpcd/ens3.pid`），而 NetworkManager 因为 `[ifupdown] managed=false` 并不接管这块网卡。
这时面板对 WAN 的 IPv6 / MTU 设置写了也不生效，PD 模式会直接拒绝启动（否则会杀掉这个实例、断掉 WAN 的 IPv4 地址）。

移交某块网口给 NetworkManager：

```bash
sudo cp /etc/network/interfaces /etc/network/interfaces.bak
sudo sed -i 's/^allow-hotplug ens3/# allow-hotplug ens3/; s/^iface ens3 inet dhcp/# iface ens3 inet dhcp/' /etc/network/interfaces
sudo systemctl stop ifup@ens3.service
sudo systemctl restart NetworkManager
nmcli -t -f DEVICE,STATE,CONNECTION device status   # 连接名不应再是 Ifupdown (ens3)
```

> ⚠️ 该网口会短暂断网，从 WAN 侧 SSH 操作会掉线；请从 LAN 侧或本地控制台执行。

确认 PD 实例没有和别人抢网卡：

```bash
systemctl status linux-router-pd --no-pager
ls -l /run/dhcpcd/
ps -o pid,args -C dhcpcd
```

正常情况下只有一个 dhcpcd 进程，命令行形如
`dhcpcd -6 -B -q -f /etc/linux-router/dhcpcd.conf <WAN> <LAN>`。
如果还看到发行版的 `dhcpcd: <网卡> [ip4] [ip6]`，说明有另一套在管 IPv4，先按上表定位来源。

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

### PD 拿不到前缀

```bash
systemctl status linux-router-pd --no-pager
journalctl -u linux-router-pd -n 80 --no-pager
cat /var/lib/linux-router/pd.json
sudo tcpdump -i <WAN接口> -nv 'ip6 and (port 546 or port 547)'
```

依次确认：

1. WAN 选的是逻辑接口（PPPoE 是 `ppp0`，不是 `enp1s0`）；
2. 抓包里 Advertise 是否带 `IA_PD`（不带就是运营商没给前缀，只能用 ULA + NAT66）；
3. `pd.json` 里 `delegated` 有值但 `prefix` 为空，说明 hook 没跑或 LAN 没拿到地址，看 `journalctl -u linux-router-pd` 与 `ip -6 addr show dev <LAN>`；
4. 光猫是否真的是桥接（路由模式下前缀会被光猫吃掉）。

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

默认卸载保留账号、密钥和路由配置，删除 `/etc/nftables.d/90-linux-router.nft` 与 `/etc/dnsmasq.d/linux-router.conf`，并恢复安装器记录的 `nftables.conf`、`dnsmasq.conf`、NetworkManager、netplan、sysctl、IPv4/IPv6 转发以及 `dhcpcd`、`systemd-resolved` 状态。使用 `--purge-data` 会删除全部运行数据，且不可恢复。

通过 SSH 卸载时，网络运行状态可能被延迟恢复。确认维护窗口后，使用：

```bash
sudo bash install.sh uninstall \
  --install-dir /home/router-panel \
  --data-dir /home/router-panel/data \
  --apply-network-now
```

如果安装或升级在健康检查前失败，安装器会尝试恢复应用目录、数据目录、项目网络配置和服务状态。恢复后仍应检查 `systemctl`、`nmcli` 和默认路由。

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
