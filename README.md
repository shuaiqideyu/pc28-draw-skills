# 加拿大 Keno

BCLC 与 PlayNow 的开奖，换成 PC28 三码。

[Telegram @yuuu](https://t.me/yuuu)

<br>

## 来源

| 站点 | 角色 | 访问 |
|---|---|---|
| BCLC | 开奖源头 | 推荐加拿大 IP |
| PlayNow | BCLC 旗下消费站，同一套开奖 | 推荐加拿大 IP |
| [yu28](https://yu28.top/) | 作者自营 | 任意 IP，免费，不限次数 |
| [pc28.help](https://pc28.help/) | 作者自营 | 任意 IP，免费，不限次数 |

BCLC 与 PlayNow 是同一后端、同一套期号、同一份 JSON。两个自营站已经把时间转成北京时间，普通 HTTPS 就能拉。

## 怎么接

开盘推荐主用 BCLC 与 PlayNow，备用 yu28 与 pc28.help。官方源被挡住或封盘时，自营站补这一期。

个人项目可以直接把 yu28 和 pc28.help 当作主连接，不必自备加拿大 IP。

<br>

## 接口

### 主接口

开奖最快。一个 JSON 对象。

`num` 是原始开奖顺序，还没有排序。`nextKenoDraw` 是下一期倒计时。

> 封盘、维护或卡奖时，`num` 会变成 20 个 0。这不是开奖，丢掉。

```text
https://www.bclc.com/services2/keno/nextdraw/
https://www.playnow.com/services2/keno/nextdraw/
```

### 近 10 期

比主接口慢 1–2 秒。封盘时主接口已经是 0，这里往往还有真实号码，用来兜底。

JSON 数组，最近 10 期。`drawNbrs` 已经升序。时间是 BC 本地、12 小时制，例如 `Jun 20, 2026` / `07:11:30 PM`。

```text
https://www.bclc.com/services2/keno/draw/latest/10
https://www.playnow.com/services2/keno/draw/latest/10
```

### 指定期号

跳期时补那一期。和近 10 期同一 schema，数组长度为 1，末尾 `/1` 固定写 1。

```text
https://www.bclc.com/services2/keno/draw/期号/1
https://www.playnow.com/services2/keno/draw/期号/1
```

### 自营

yu28 在 [yu28.top](https://yu28.top/) 注册后创建 Key，调用不限次数。说明见 [文档](https://yu28.top/docs)。

```text
https://yu28.top/api/keno.json?nbr=1
```

pc28.help 不用 Key。

```text
https://pc28.help/api/keno.json?nbr=1
```

<br>

## 请求

BCLC 与 PlayNow 必须带完整的 Chrome 请求头。Akamai 会看指纹。推荐 HTTP/2，并走加拿大 IP。自营站不需要这套指纹。

| | 地址族 | 说明 |
|---|---|---|
| BCLC | IPv6 | 同一套指纹下，比 IPv4 大约快 1 秒 |
| PlayNow | IPv4 | 没有 IPv6。用 Chrome 指纹直接访问 |

<br>

## 时间

接口上的时间都是不列颠哥伦比亚当地墙钟，字符串里没有时区。人们也把它叫作 `America/Vancouver`。

省政府已经改为全年夏令时。2026 年 3 月 8 日的拨快是最后一次，11 月 1 日起不再回拨。太平洋时间固定为 **UTC−7**，北京时间 **+15 小时**。

[Adopting permanent daylight saving time](https://archive.news.gov.bc.ca/releases/news_releases_2024-2028/2026AG0013-000209.htm) · 2026-03-02

墙上的钟如果还在跳，偏移只在两档之间：夏令时 UTC−7（北京 +15 小时），冬令时 UTC−8（北京 +16 小时）。这两档会来回切。不要写死，也不要只绑 `America/Vancouver` 或 `America/Los_Angeles` 其中一口。

期号连续时，用上一期墙钟对照。

| 墙钟 | 怎么处理 |
|---|---|
| 大约 +3.5 分钟 | 正常下一期 |
| 相对这个步进突然多了或少了大约 1 小时 | 时令切换。绝对时间仍按约 210 秒往前，只把偏移改 1 小时 |
| 大约 36 分钟 | 维护空档。墙钟和绝对时间一起走，不要当成时令切换 |

例：01 期 09:03，02 期写成 08:06，是回到 UTC−8。09:58 的下一期写成 11:01，是拨到 UTC−7。

已经按期号记下的时间，不要用新偏移改写。

<br>

## 三码

20 个球先按升序排列。范围 1–80，位置从 1 数。

位 1（最小）和位 20（最大）不参与。

```text
num1 = (位2 + 位5 + 位8 + 位11 + 位14 + 位17) % 10
num2 = (位3 + 位6 + 位9 + 位12 + 位15 + 位18) % 10
num3 = (位4 + 位7 + 位10 + 位13 + 位16 + 位19) % 10
sum  = num1 + num2 + num3
```

<br>

## 运行

```bash
git clone https://github.com/shuaiqideyu/pc28-draw-skills.git
cd pc28-draw-skills
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
python -m collector
```

<br>

[MIT](LICENSE)
