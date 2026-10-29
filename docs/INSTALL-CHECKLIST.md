# 上机清单：用 Linux Router 替代 iKuai

面向「Debian 13 最小化安装 + 工控机替换现有软路由」的一次性操作清单。
按顺序做，每一步都有可验证的结果；标 ⚠️ 的地方是容易翻车的位置。

> 全程建议在旁边接一台显示器 + 键盘（或保留 iKuai 的 VM 控制台）。
> 应用路由配置会改 IP，远程操作一定会断一次。

---

## 0. 动手前先留后路

- [ ] **导出 iKuai 配置备份**（系统设置 → 备份），存到另一台机器
- [ ] 抄下现有参数，等下要照着填：

| 参数 | 你的值 |
| --- | --- |
| WAN 方式（PPPoE / DHCP / 静态） | |
| PPPoE 账号 / 密码 | |
| LAN 网段（如 192.168.9.0/24） | |
| DHCP 地址池 | |
| 静态绑定 / 端口映射 | |
| 限速（上行 / 下行 Mbit） | |

- [ ] 准备 U 盘：放一份 `Linux-Router-debian13-nftables.zip`
- [ ] 确认工控机网口数量与顺序（`eth0/eth1` 还是 `enp1s0/enp2s0`），贴个标签
- [ ] 想清楚回滚方式：**保留 iKuai 不动，接线切换失败就插回去**

---

## 1. 最小化安装时的三个选择

### 1.1 不要勾选任何 task

走到「软件选择」时，**取消勾选** `Debian desktop environment`、`GNOME`、`web server`、`SSH server`、`standard system utilities` —— 全部不选。

> 勾了 `standard system utilities` 会装一堆用不到的东西；本项目只需要下面第 3 步显式装的那几个包。

### 1.2 root 密码：设还是不设，决定你有没有 sudo

| 你的选择 | 结果 | 装完怎么提权 |
| --- | --- | --- |
| **设置了 root 密码** | 安装器**不会**装 sudo，普通用户**不在** sudo 组 | `su -` 输入 root 密码 |
| **root 密码留空** | 强制创建一个用户，并自动装 sudo、把该用户加入 sudo 组 | `sudo -i` 或 `sudo bash ...` |

⚠️ 这就是你说的「sudo 和普通用户权限组有问题」的根源：**没装 sudo 时，`sudo` 组根本不存在**，
所以 `usermod -aG sudo <用户>` 会报 `group 'sudo' does not exist`。顺序必须是「先装 sudo，再加组」。

### 1.3 软件源只留网络镜像

安装器会让你配 apt 源。**把 DVD/CD 那一项去掉，只留一个国内镜像**（163、清华、中科大）。

即使没去掉也不致命 —— 本项目的安装器会自动注释掉 `deb cdrom:` 行（见第 2.3 节）。

---

## 2. 装完系统，先做这四件事

### 2.1 切到 root

```bash
id -u                      # 输出 0 说明你已经是 root
su -                       # 不是 0 就用这个，输 root 密码
```

### 2.2 检查时间（很重要，但最容易被忽略）

```bash
date
timedatectl
```

时间不对会导致所有 HTTPS 请求失败（证书校验），表现是 `curl` 报 `SSL certificate problem`，
或者 `apt update` 报 `Release file is not valid yet`。

```bash
timedatectl set-ntp true
timedatectl status | grep -i synchronised
```

### 2.3 修掉光盘源

DVD 安装会往 `/etc/apt/sources.list` 写一行 `deb cdrom:[...]`。光盘一拔，`apt update` 就报：

```
E: The repository 'cdrom://[Debian GNU/Linux 13.x ...] trixie Release' does not have a Release file.
```

先确认它在哪：

```bash
grep -rn "cdrom:" /etc/apt/sources.list /etc/apt/sources.list.d/ 2>/dev/null
```

手动注释（也可以用本项目的安装器，它会自动处理）：

```bash
sed -i -E -e 's|^([[:space:]]*)deb cdrom:|# \1deb cdrom:|' \
          -e 's|^([[:space:]]*)deb-src cdrom:|# \1deb-src cdrom:|' /etc/apt/sources.list
apt update; echo "exit=$?"        # 期望 exit=0
```

> 如果源是 deb822 格式（`/etc/apt/sources.list.d/*.sources` 里有 `URIs: cdrom:`），
> 安装器只会告警，需要你手动删掉那一段。

### 2.4 确认网络与 DNS

```bash
ip -br addr                       # 至少一块网口有地址
ping -c2 223.5.5.5                # 通外网 IP
getent hosts deb.debian.org       # DNS 正常
```

DNS 不通但能 ping 通 IP，说明 `/etc/resolv.conf` 是空的：

```bash
printf 'nameserver 223.5.5.5\nnameserver 119.29.29.29\n' > /etc/resolv.conf
```

> 装好本项目后，`/etc/resolv.conf` 会由面板按 WAN 下发的 DNS 自动维护，不用再管。

---

## 3. 装 sudo（可选，但强烈建议）

只在第 1.2 节选了「设置 root 密码」时才需要：

```bash
apt install -y sudo
usermod -aG sudo <你的用户名>      # 现在 sudo 组存在了
```

**必须重新登录**（或 `newgrp sudo`）才生效，验证：

```bash
su - <你的用户名>
sudo -v && echo "sudo OK"
```

---

## 4. 部署本项目

### 4.1 没有 curl 怎么办

最小化安装**不带 curl**（自带的是 wget）。两种办法：

```bash
apt install -y curl ca-certificates      # 有网络时
```

或者用 U 盘（无需任何网络）：

```bash
mkdir -p /root/lr && cd /root/lr
python3 -m zipfile -e /media/usb/Linux-Router-debian13-nftables.zip .
```

### 4.2 安装（三选一）

```bash
# A. 本地源码（最稳）
cd /root/lr/Linux-Router
bash install.sh install --source-dir /root/lr/Linux-Router

# B. 加速脚本（有网络但 GitHub 慢）
curl -fsSL https://raw.githubusercontent.com/wangyr2008/Linux-Router/main/get.sh -o /tmp/lr.sh
bash /tmp/lr.sh install

# C. 走镜像
bash /tmp/lr.sh install --mirror https://ghfast.top/
```

安装器会自动：装依赖 → 注释光盘源 → 部署 `/opt/linux-router` → 建服务账号 →
写 nftables/dnsmasq 引导配置 → 开转发 → 启服务 → 健康检查。

⚠️ 安装阶段**不会动你的网卡**，网口是在面板里点「应用」时才接管的。所以装完网络不会断。

### 4.3 记下初始密码

安装最后会打印：

```
Initial login:
  Username: admin
  Password: xxxxxxxx
Credentials are also stored in /var/lib/linux-router/initial_password.txt
```

---

## 5. 首次配置（这一步会断线）

### 5.1 先用 LAN 口调通

最稳的顺序：**工控机 WAN 口先接爱快的 LAN 口**（走 DHCP），笔记本接工控机 LAN 口。

```bash
ip -br link                       # 记下两块网口的名字
```

不确定哪个是哪个？拔掉一根网线看 `ip -br link` 里谁的 `NO-CARRIER` 消失。

浏览器打开 `http://<当前IP>/` → 登录 → **路由** 页：

1. WAN 连接方式：**DHCP**，WAN 网口选接爱快的那块
2. LAN 网口：接笔记本的那块。**网口多于两个时可以在这一步多选**，
   选两个及以上会自动桥接成 `br-lan`（当成一台交换机用），此时 LAN 的接口名会变成 `br-lan`
3. LAN IPv4 网段：先别改，保持 `192.168.50.0/24`
4. IPv6 模式：先选 **ULA + NAT66**（稳定、不依赖运营商）
5. 访问控制：**开放**（此时在 LAN 侧操作，先别开保护）
6. 点「应用路由配置」

⚠️ 应用后 LAN 口地址变成 `192.168.50.1`，当前页面会掉线。把笔记本改成自动获取，
访问 `http://192.168.50.1/`。

### 5.2 验证基础功能

```bash
# 在接 LAN 口的笔记本上
ip route                 # 默认路由应是 192.168.50.1
ping -c2 192.168.50.1
ping -c2 223.5.5.5
curl -s -o /dev/null -w "%{http_code}\n" http://www.baidu.com
```

面板「路由」页的 `IPv4 NAT` 计数应该开始增长（说明流量真的走了 masquerade）。

---

## 6. 正式切换（替换爱快）

⚠️ 这一步会断网，**不要在只有远程访问的情况下做**。

1. **先按第 5 节把工控机调通**（此时是「爱快 → 工控机」双层 NAT，能上网）
2. 如果需要 PPPoE：光猫改桥接，工控机 WAN 直连光猫，面板里 WAN 改成 PPPoE 填账号密码
3. 断电爱快（或拔掉它的网线），把光猫/入户线直接插工控机 WAN 口
4. LAN 口接你的交换机
5. 检查：面板能否打开、设备能否上网、DNS 是否正常

**失败就回滚**：工控机断电，插回爱快，通电。工控机的配置不会丢。

### 切 IPv6 到运营商 PD（可选，之后再做）

确认光猫桥接、本机拨号后：

1. 面板 → 路由 → IPv6 模式 → **运营商 PD 自动** → 应用
2. 依赖页看「DHCPPrefixDelegation」是否拿到前缀
3. 拿到后 **立刻把访问控制改成「保护」**——公网 IPv6 下 LAN 设备是直接暴露的

---

## 7. 验收清单

装完逐项打勾：

- [ ] 面板能登录，7 个页面都能打开
- [ ] 依赖页：除「入站访问控制 = 开放」外全部正常
- [ ] LAN 设备能拿到 `192.168.50.x` 地址，能上网，能解析域名
- [ ] `nft list chain ip lr_nat postrouting` 有 masquerade，计数器在涨
- [ ] 需要 IPv6 的话：LAN 设备有 IPv6 地址，`ping6 2400:3200::1` 通
- [ ] 限速生效：`tc qdisc show dev <WAN>` 显示 `cake ... bandwidth <你的值>Mbit`
- [ ] 静态绑定生效：绑一台设备后重新连接，拿到固定 IP
- [ ] **重启一次工控机**，等 1 分钟，确认网络自动恢复、面板能打开
- [ ] 设置页导出一次备份，存到别的机器

重启验证很关键——它同时检验 `systemctl is-enabled`、`linux-router-dns.timer`、
`linux-router-cake.timer` 是否都正常。

---

## 8. 常见坑速查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| `sudo: command not found` | 最小安装没装 sudo | `su -`，或 `apt install sudo` 后再加组 |
| `usermod: group 'sudo' does not exist` | 同上，组还没创建 | 先 `apt install sudo` |
| `apt update` 报 cdrom 没有 Release 文件 | DVD 源 | 注释 `deb cdrom:`（安装器会自动做） |
| `curl: command not found` | 最小安装不带 curl | `apt install curl` 或用 U 盘 + `python3 -m zipfile` |
| `Permission denied: install.sh` | 没有执行权限 | 用 `bash install.sh ...`，不要 `./install.sh` |
| `SSL certificate problem` | 系统时间不对 | `timedatectl set-ntp true` |
| 装完面板打不开 | 80 端口被占/服务没起 | `ss -ltnp \| grep :80`；`systemctl status router-panel` |
| 应用配置后页面掉线 | LAN 地址变了 | 用新地址重开（这是正常的） |
| 面板和 SSH 都连不上 | 保护模式没放行端口 | 控制台执行 `nft delete table inet lr_input` |
| 网卡名字和爱快里看到的不一样 | 命名规则不同 | `ip -br link` + 插拔网线确认 |
| 装完 IPv6 不通 | 选了 ULA 但没开 NAT66 | 面板确认「启用 IPv6 NAT66」 |
| PPPoE 拨不上 | 光猫还在拨号 / 账号错 | 光猫改桥接；`journalctl -u linux-router-pppoe -f` |

---

## 9. 装完之后

```bash
# 升级（不动网络配置和数据）
bash /root/lr/Linux-Router/install.sh upgrade --source-dir /root/lr/Linux-Router

# 卸载（保留账号与配置）
bash /root/lr/Linux-Router/install.sh uninstall

# 彻底删除
bash /root/lr/Linux-Router/install.sh uninstall --purge-data
```

日常备份走面板「设置 → 备份与恢复」，导出的 JSON 里有宽带密码，别放公共位置。
