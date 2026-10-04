# 开发踩坑记录

> 本文档记录项目开发过程中**实际踩到并修复**的问题。
> 每一条都包含：现象 → 根因 → 修复 → 为什么难发现。
> 这些问题不是从教程里抄的，而是在真实运行中被测试与实测逼出来的。

---

## 1. ★★ 非重入锁的自杀式死锁（同一类问题出现两次）

### 现象

独立脚本（如 `scripts/cleanup_demo_data.py`）运行到第一次访问数据库时
**静默卡死** —— 没有任何报错、没有任何日志、进程永远挂起。

### 根因

```python
_init_lock = threading.Lock()          # 非重入锁

def get_engine():
    if _engine is None:
        with _init_lock:               # 获取锁
            _engine = _build_engine()

def get_session_factory():
    if _session_factory is None:
        with _init_lock:               # 先获取了同一把锁
            _session_factory = sessionmaker(
                bind=get_engine(),     # 又进去想获取同一把锁 → 永久阻塞
            )
```

`threading.Lock` **不可重入**：同一个线程第二次 `acquire()` 会永久阻塞，
而不是抛异常或返回。

调用链：`session_scope()` → `get_session_factory()` → 【持锁】→ `get_engine()` → 【等锁】→ 死。

### 同一类问题的第二处

```python
def ensure_ready(self):
    with self._lock:                   # 持有锁
        vectors = self._request(...)   # → _get_client()

def _get_client(self):
    if self._client is None:
        with self._lock:               # 再次获取同一把锁 → 死
            self._client = httpx.Client(...)
```

只在「首次探测远程嵌入维度」时触发。

### 修复

**结构性修复：绝不在持有锁的时候，去调用另一个会加同一把锁的函数。**

```python
def get_session_factory():
    if _session_factory is None:
        engine = get_engine()          # ✅ 先在锁外把需要的东西取出来
        with _init_lock:
            _session_factory = sessionmaker(bind=engine, ...)
```

第二处则改为**构造时就创建 HTTP 客户端**（`httpx.Client` 的构造不会发起网络请求，
提前创建零成本），彻底消除那条加锁路径。

### 为什么极难发现

- **Web 服务完全正常**：lifespan 启动时会先调 `check_connection()` → `get_engine()`，
  等请求进来时 Engine 早已创建，`if _engine is None` 为假，**根本不会去碰锁**。
- **测试也正常**：`TestClient` 的 lifespan 同样会预热。
- 只有「独立脚本的第一个动作就是 `session_scope()`」才会触发 —— 而独立脚本通常不是主流程。
- 表现形式是**静默挂起**，没有堆栈、没有日志，看上去像"数据库连不上"，
  容易往错误方向排查（我一开始就怀疑是 MySQL 锁或网络）。

### 回归测试

`tests/test_db.py::test_session_scope_does_not_deadlock_before_engine_created`

关键手法：**放在后台线程里跑并设置超时**。
如果直接在主线程调用，死锁时整个测试进程会卡住，连失败信息都拿不到。

---

## 2. 配置被环境变量静默覆盖

### 现象

`.env` 里认真填好的 `DEFAULT_LLM_BASE_URL` 读出来是空字符串，
但同一个文件里的 `DEFAULT_LLM_API_KEY` 却正常。

### 根因

pydantic-settings 的优先级是 **环境变量 > .env**。
运行环境里恰好存在同名的环境变量（值为空），把 `.env` 覆盖了。

排查发现 **41 个配置项里有 33 个**与系统环境变量同名 ——
`HOST`、`PORT`、`DEBUG`、`APP_NAME` 这类通用名在任何真实部署环境里都可能撞车
（CI 系统、Docker、IDE、其他 CLI 工具、外层 wrapper）。

### 修复

给所有配置加命名空间前缀 **`HNE_`**（Hetero Narrative Engine）：

```ini
HNE_MYSQL_PASSWORD=***
HNE_DEFAULT_LLM_BASE_URL=https://api.deepseek.com
```

之后只有 `HNE_XXX` 会被读取，且部署时仍可用 `HNE_PORT=9000` 正常覆盖。

### 为什么难发现

不报错，只是配置**莫名其妙不生效**。如果没写断言、只看"程序能跑"，
会一直以为是代码问题。

---

## 3. 推理模型吃光输出配额，产生"静默空回复"

### 现象

调用 `deepseek-flash` 返回**空字符串**，而 `finish_reason` 却是 `length`、
HTTP 200 —— 看起来是成功，实际什么都没拿到。

### 根因

```json
message 的键: ['role', 'content', 'reasoning_content']
  content            : ""            ← 正文一个字都没有
  reasoning_content  : "我们需要回答用户……"
  usage.completion_tokens_details = {'reasoning_tokens': 200}
```

推理模型**先把输出配额花在思考过程上**。`max_tokens=200` 全被思考吃光，
正文还没开始生成，配额就用完了。

实测一次普通叙事描述：`completion_tokens=433`，其中 `reasoning_tokens=380`
—— **思考占了 88%**。

### 修复

1. **拦截空回复**，抛出可操作的错误而不是让它静默入库：
   ```
   模型输出被 max_tokens 截断，且没有产生正文（该模型是推理模型，思考过程占用了全部输出配额）
   修复建议：请调大 max_tokens；推理模型建议至少 1024
   ```
2. `TokenUsage` 增加 `reasoning_tokens` 字段，单独统计思考开销
3. `ChatResult` 增加 `reasoning` 字段（原先只有流式接口能拿到思考内容，两边能力不对称）
4. 健康检查对推理模型做特殊判定：思考吃光配额时仍判定**连接正常**
   （网络通、鉴权过、模型存在都已被证明，不该误报配置错误）

### 界面约定

**「最大输出 Token」包含思考过程** —— 这条必须写在界面上。
用户设 500，推理模型可能只剩 50 个字。与 SillyTavern（酒馆）的默认规则一致。

---

## 4. 思考强度被厂商"接受但忽略"

### 现象

`deepseek-flash` 接受 `reasoning_effort` 参数（不报错），但**完全不理它**：

| 思考强度 | 输出 token | 思考 token |
|---|---|---|
| auto | 218 | 153 |
| high | 235 | 172 |
| low | 183 | 133 |
| off | 254 | **175** |

设成「尽量关闭」，思考反而更多。

### 为什么比返回 400 更危险

用户以为「深度思考已开启」，实际毫无变化，且**没有任何提示**。

### 修复

提供**参数生效性探测**：用同一提示、`temperature=0`，
分别以 `off` 和 `high` 各调用一次，比较思考 token 数。

判定阈值定为 **40%**，不是 15% —— 因为推理模型的思考长度本身就有
**±15% 的自然波动**（实测同条件连续 4 次：153 / 172 / 133 / 175）。
阈值定低了会把噪声误判成「参数生效」。

> 我第一次就把阈值定成了 15%，真实探测得到 20% 差异，判定为「已生效」，
> 但手工实验里同一配置的差异是 0%。加上方向判断和更高阈值后才收敛到正确结论。

探测结论会**持久化到配置上**（含「探测时用的模型名」），
换模型后旧结论自动失效，界面据此给出基于实测的警告。

---

## 5. ChromaDB 重新打开集合时悄悄注入默认嵌入函数

### 现象

用 `embedding_function=None` 创建的集合，**重新打开时** `collection._embedding_function`
变成了一个 `DefaultEmbeddingFunction` 实例。

### 风险

如果代码某处忘记显式传 `embeddings`，ChromaDB 会用**默认的 ONNX 模型**生成向量，
与库里已有向量不在同一语义空间 —— **不报错，但检索结果全错**。

### 修复

三条铁律：

1. 集合一律以 `embedding_function=None` 创建
2. 所有 `add` / `query` 都**显式传入向量**，绝不依赖 ChromaDB 自动嵌入
3. 集合元数据里记录「嵌入指纹」（后端 + 模型 + 维度），打开时校验；
   不一致直接报错并给出修复建议

---

## 6. 函数名遮蔽导致的 `unexpected keyword argument`

### 现象

```
TypeError: create_provider() got an unexpected keyword argument 'provider_type'
```

### 根因

`app/services/provider_service.py` 里：

```python
from app.llm import create_provider        # 导入工厂函数

def create_provider(db, user_id, payload): # 又定义了同名服务函数
    ...

# 后面想调用工厂：
provider = create_provider(provider_type=..., ...)   # ← 实际调到了自己，参数对不上
```

Python 里后定义的同名函数会**直接覆盖**先导入的那个，不报错。

### 修复

```python
from app.llm import create_provider as create_llm_provider
```

并在注释里说明原因，避免后来人again踩。

---

## 7. 跨字段校验放在错误的层，导致 500 而不是 422

### 现象

提交 `max_tokens >= context_window` 的配置，用户收到 **500 内部错误**，
而不是清晰的参数校验错误。

### 根因

`ProviderCreate` 是独立模型，没有校验 `max_tokens` 与 `context_window` 的关系；
这个约束原本只在组装 `ProviderConfig` 时才触发，抛出的是 pydantic 的
`ValidationError`，被全局处理器当成未捕获异常 → 500。

### 修复

两层都要校验：

1. **接口层**：`ProviderCreate` 加 `model_validator`，快速反馈 422
2. **服务层**：写库前拿**最终组合**再查一次 ——
   因为 PATCH 是部分更新的，用户只提交 `max_tokens` 时
   `context_window` 沿用数据库旧值，接口层根本看不到完整组合

---

## 8. bcrypt 的 72 字节限制

### 现象

64 个汉字的密码会被 bcrypt 静默截断，导致
「前 72 字节相同即视为同一密码」的安全问题。

### 修复

按**字节数**而不是字符数校验：

```python
if len(password.encode("utf-8")) > 72:
    raise ValueError("密码过长：中文一个字约 3 字节，即约 24 个汉字")
```

如果只写 `max_length=72`，Pydantic 会按**字符数**判断 —— 24 个汉字（72 字节）
能过，但 25 个汉字（75 字节）的校验结果与 bcrypt 实际行为不一致。

---

## 9. 密钥相关的三个静默风险

| 风险 | 处理 |
|---|---|
| API Key 明文入库 | Fernet 对称加密后存储 |
| 响应/日志泄露明文 | 统一走 `mask_api_key()`，只回显 `sk-8****d0ec` |
| 越权访问他人配置 | 每个查询强制带 `user_id`；且返回 **404 而非 403** —— 403 会暴露「这个 ID 确实存在」，可被用来枚举 |

另外：**「用户不存在」与「密码错误」必须返回完全相同的信息**，
且用户不存在时也要跑一次等价的 bcrypt 校验 ——
否则响应速度差异同样能被用来枚举有效用户名（计时攻击）。

---

## 10. Windows 上 stdout 块缓冲让排查方向跑偏

### 现象

脚本卡住时，输出文件里只有第一行，让人以为是死锁发生在更早的位置。

### 根因

Windows 上 stdout 重定向到管道/文件时是**块缓冲**。
脚本被强制终止时，缓冲区里的内容全部丢失。

### 修复

所有诊断/脚本输出一律 `print(..., flush=True)`。

---

## 11. ORM 默认「置 NULL」与外键的非空 + 级联删除冲突

### 现象

删除一张**已被叙事会话使用**的角色卡时直接报错：

```
IntegrityError (1048, "Column 'character_card_id' cannot be null")
[SQL: UPDATE narrative_sessions SET character_card_id=%s, updated_at=now()
      WHERE narrative_sessions.id = %s]
```

奇怪的是：删除一张新建的、没有任何会话的角色卡，一切正常。

### 根因

数据库层面写的是 `ON DELETE CASCADE`，看起来「删父表就自动删子表」，没问题。
但 SQLAlchemy 在 ORM 层有**自己的一套默认行为**：

> 删除父对象时，它会把子对象的外键**置为 NULL**（术语叫 nullify），
> 而不是交给数据库去级联。

而 `narrative_sessions.character_card_id` 是 `NOT NULL`
（一张卡就是一本书，会话不可能不挂在任何卡上），
于是 SQLAlchemy 发出的 `UPDATE ... SET character_card_id = NULL` 直接违反非空约束。

数据库里明明写了 `ON DELETE CASCADE`，却根本轮不到它生效 ——
**ORM 抢在数据库前面，做了件错事。**

### 修复

给关系加上 `passive_deletes=True`，告诉 SQLAlchemy「别管，交给数据库」：

```python
sessions: Mapped[list["NarrativeSession"]] = relationship(
    back_populates="character_card",
    cascade="all, delete-orphan",
    passive_deletes=True,        # ← 关键
)
```

### ★ 同一个项目里，另一张表必须**反着写**

`llm_providers.sessions` 看起来是一模一样的代码，但它**故意不加** `passive_deletes`：

| 表 | 外键列 | 外键动作 | 想要的语义 | 写法 |
|---|---|---|---|---|
| `llm_providers` | 可空 | `SET NULL` | 删配置，**故事要保留** | 用默认（置 NULL）✅ |
| `character_cards` | 非空 | `CASCADE` | 删卡，**故事一起删** | 必须 `passive_deletes=True` ✅ |

也就是说：**这里的默认行为恰好是对的，那里的默认行为恰好是错的。**
所以两张表都不能靠推理，只能真的删一次来验证：

```
删除模型配置后 -> 会话存在: True  | llm_provider_id = None
删除角色卡后   -> 会话存在: False
```

### 为什么难发现

- 只有「删除一张**已被使用**的卡」才触发。开发时先建卡、再删卡的自测路径
  完全正常，问题要等到真正开过会话之后才暴露。
- 数据库 DDL 里 `ON DELETE CASCADE` 写得清清楚楚，
  看模型定义时会想「级联已经配好了」，不会再去怀疑 ORM 层。
- 报错信息指向 `character_card_id cannot be null`，
  第一反应是「代码哪里传了 None」，而不是「ORM 在帮我置 NULL」。

### 回归测试

- `test_delete_card_force_cascades_sessions` —— 删卡后会话必须真的消失
- `test_delete_provider_keeps_narrative_sessions` —— 删模型配置后**故事必须还在**

两条测试一正一反，把这个「默认行为对不对取决于外键语义」的坑钉死。

### ★ 后续演进：这个修复本身后来被推翻了

值得把这段记录下来，因为它说明「修好一个 bug」和「设计对不对」是两件事。

上面的修法（`passive_deletes=True` + 保留 `CASCADE`）在**技术上是正确的**，
但它固化了一个有问题的设计：**删一张角色卡就把用户的故事全部删光**。

后来又发现两件事，最终把整个语义反转了：

1. 同一个项目里，删模型配置会**保留**故事，删角色卡却**删光**故事 ——
   同样是「删掉一个依赖」，行为却相反，用户无法预期。
2. 用户提出：删除时应该让他自己勾选「是否一并删除对话记录 / 世界书」。

于是外键改成了 **`ON DELETE SET NULL`（可空）**，默认行为变成
「保留故事，只解除关联」，要不要删由接口参数决定：

| | 改造前 | 改造后 |
|---|---|---|
| 外键 | `CASCADE` + 非空 | `SET NULL` + 可空 |
| 删卡的默认效果 | 故事全被删掉 | 故事保留，只解除关联 |
| 关系配置 | `cascade="all, delete-orphan"` + `passive_deletes=True` | 去掉 `delete-orphan`，保留 `passive_deletes` |
| 删除时机 | 数据库级联自动删 | service 层按用户勾选显式删 |

**这里最值得记住的一点**：`passive_deletes=True` 这个参数在两次改造中都保留了，
但它配合的外键动作完全不同 ——
一句「加上 passive_deletes 就好了」的经验，换个外键语义就变成了错的。
**参数的写法必须和数据库的 `ON DELETE` 动作对上，不能背下来照抄。**

这也是为什么两张表（`llm_providers` 与 `character_cards` → `sessions`）
看起来一样的代码，现在终于**语义一致**了：都是「置空、保留数据」。

---

## 12. 为兼容 A 而做的文本处理，误伤了 B

### 现象

从 PNG 导入角色卡时，「成功」了，但文本内容悄悄变了：

```
期望: "Hello there, traveller."
实际: "Hellothere,traveller."
```

所有空格都没了。中文卡片因为本身很少用空格，一开始完全没暴露。

### 根因

PNG 里卡片数据是 **base64** 编码的，而有些工具输出的 base64 会按 64 列**折行**。
所以解析时要先把换行去掉：

```python
cleaned = "".join(text.split())     # 去掉所有空白
decoded = base64.b64decode(cleaned)
```

这段逻辑本身没错。问题在于我**把 `cleaned` 复用到了后面的裸 JSON 分支**：

```python
candidates.append(base64.b64decode(cleaned)...)   # ✅ 用去空白的版本，对
candidates.append(cleaned)                        # ❌ 裸 JSON 也用了它 —— 错
```

base64 的「去空白」是**安全**的（base64 字符集里根本没有空格，去掉了不影响语义）。
但 JSON 原文里的空格**是有意义的**，它是字符串内容的一部分。
同一段文本，两种用途，对空白的容忍度完全相反 —— 我却当成了一回事。

### 修复

区分两种用途，各用各的：

```python
stripped = text.strip()             # 裸 JSON 用这个（只去首尾）
compact  = "".join(text.split())    # 仅 base64 用这个（去掉全部空白）

candidates.append(base64.b64decode(compact)...)
candidates.append(stripped)         # ← 原文
```

### 为什么难发现

- **破坏是静默的**：接口返回 201，字段也都在，只有内容不对。
- 中文文本几乎不用空格，所以中文用例全部通过。
  换成一句英文才立刻现形 —— 这也说明**测试数据的语言多样性**是有价值的。
- 报错和异常一个都没有，靠日志和状态码永远查不出来，
  只有「断言具体内容」的测试才能抓到。

### 回归测试

- `test_import_png_accepts_raw_json_text_chunk` —— 裸 JSON 路径的空格必须保留
- `test_import_png_preserves_spaces_in_base64_card` —— base64 路径同样要保留，
  并顺带断言换行 `\n` 不被压掉

---

## 13. MySQL 不支持 `LIMIT` 出现在 `IN (子查询)` 里

### 现象

会话列表接口（要显示每个会话「最后一条消息」的预览）直接 500：

```
pymysql.err.NotSupportedError: (1235, "This version of MySQL doesn't yet support
'LIMIT & IN/ALL/ANY/SOME subquery'")
```

SQLAlchemy 生成的是「相关子查询 + `IN`」这种写法：

```python
latest = (
    select(Message.id)
    .where(Message.session_id == NarrativeSession.id)   # 相关子查询
    .order_by(Message.id.desc())
    .limit(1)
    .scalar_subquery()
)
select(Message).where(Message.id.in_(latest))
```

### 根因

MySQL（5.7 与 8.0 都一样）**不允许**在 `IN / ALL / ANY / SOME` 的子查询里用 `LIMIT`。
这是 MySQL 自身的限制，PostgreSQL / SQLite 都能跑 —— 所以这段代码
在别的数据库上测是绿的，只有真连 MySQL 才炸。本项目用同步 SQLAlchemy + MySQL，
必须按 MySQL 的脾气写。

### 修复

改成「先分组取 max(id)，再按这批 id 取消息」两步，MySQL 全版本都支持，
而且正好走 `(session_id, id)` 复合索引：

```python
latest_ids = (
    select(func.max(Message.id))
    .where(Message.session_id.in_(session_ids))
    .group_by(Message.session_id)
    .subquery()
)
select(Message).where(Message.id.in_(select(latest_ids.c[0])))
```

### 为什么难发现

- 单元测试测不到：接口测试用的是真 MySQL，但只有「列表接口 + 会话里真的有消息」
  这个组合才会走到这段 SQL。**空列表时 `session_ids` 为空，函数直接返回，不会报错** ——
  所以「先跑建会话、后跑列表」的用例顺序一变，结果就不一样。
- 报错信息（1235）只讲语法，不提"该怎么改"，很容易以为是 SQLAlchemy 的 bug。

### 回归测试

- `tests/test_narrative.py::test_list_sessions_is_paginated_and_sorted_by_activity`
  （先发一条消息，再查列表，并断言「最近活跃的排在最前」与预览非空）

---

## 14. ★★ 委托监听器 + 少了 `data-act`：点击毫无反应且不报错

### 现象

对话界面的「发送」按钮**点了完全没反应**：不报错、不请求、按钮也不变灰。
浏览器控制台干净得像什么都没发生。

### 根因

按钮和它的处理器是分开写的，两边靠一个属性对上：

```js
// 模板：按钮少了 data-act="send"
<button class="btn" id="btn-send" disabled>发送</button>

// 处理器：靠这个属性分发
const btn = e.target.closest('button[data-act]');
if (!btn) return;                 // ← 静默返回，什么都不说
if (btn.dataset.act === 'send') sendMessage(root, signal);
```

更迷惑的是 `closest()` 会**继续向上找**，于是它命中了工具栏上的
`data-act="prompt"`（查看提示词）按钮，把一次「发送」当成了「查看提示词」——
行为诡异到不可能靠读代码想明白。

### 修复

给按钮补上 `data-act="send"`；同时用静态测试把「判断的 act」与「声明的 act」
钉死在一起。

### 为什么难发现

- **没有任何错误**：委托监听器遇到不认识的元素就是 `return`，这是它的正常工作方式。
- pytest 完全测不到（后端接口是好的），代码审查也容易漏 ——
  这类问题只有**在真实浏览器里点一下**才会暴露。
- 本次是靠 DevTools 协议（真实无头 Edge + CDP）逐个 `Runtime.evaluate`
  才定位到的：先确认事件确实冒泡到了监听器所在的容器，再打印
  `e.target.closest('button[data-act]').dataset.act`，才发现它取到的是**另一个按钮**。

### 回归测试

- `tests/test_console.py::test_chat_view_buttons_match_their_delegated_actions`
  （静态检查：chat.js 里 `act === 'xxx'` 判断过的每个取值，都必须存在
  `data-act="xxx"` 的按钮）

---

## 15. ★ "预览"与"真正发出去的东西"走了两条代码路径

### 现象

界面上「查看提示词」显示：**注入了 2 条世界书设定**。
但同一轮真正发给模型的请求里只有 **1 条**（只有命中的那条）。
两边都"没报错"，只是数字对不上。

### 根因

关键词触发检索加进来之后，出现了两条构建路径：

```python
# 真正发请求时（engine.prepare_turn）：
scan = world_book_scanner.scan(book, history, ...)      # 只拿命中的条目
prompt = build_prompt(..., world_book_entries=scan.entries)

# 界面的「查看提示词」预览（api/v1/narrative.py 里另写的一段）：
prompt = build_prompt(card=card, history=history, world_book=book)   # ← 漏了筛选！
```

`build_prompt` 的 `world_book_entries=None` 表示"用全部启用条目"，
于是预览拿到了整本世界书。**两处代码各自演化，慢慢就不一致了。**

### 修复

把两种用途收敛到**一个**入口：

```python
# app/narrative/engine.py
def build_session_prompt(*, user_id, session_id, card, book, history) -> SessionPrompt:
    """扫描 + 召回 + 拼装，一步到位。对话与预览都只能走它。"""
```

预览改成调用 `engine.build_session_prompt(...)`，与发消息时**完全同一份代码**。

### 为什么难发现

- 两条路径都不会报错，只是内容悄悄不同 —— 没有任何异常可以抓。
- 用户往往会**拿着预览去排查"模型为什么不按设定说话"**，
  而预览本身是错的，于是排查方向从一开始就偏了，越查越迷糊。
- 后端接口测试断言的是"接口返回了什么"，不会去比对"同一轮真正发出去的请求"；

### 回归测试

- `tests/test_memory.py::test_prompt_preview_matches_the_real_request`
  （同一会话里断言：预览里的条目数 == 真实请求里的条目数）

---

## 16. ★ 把完整接口地址粘进 `base_url`，上游却报「模型不存在」

> 这是**用户实际反馈**的问题（接不通豆包与 ChatGPT），也是"错误信息把人带偏"的最佳案例。

### 现象

用户填了

```
base_url = https://ark.cn-beijing.volces.com/api/v3/responses
model    = doubao-seed-2-0-mini-260428
```

点「测试」，得到：

```
指定的模型不存在，或当前 API Key 无权访问该模型
（openai_compatible/doubao-seed-2-0-mini-260428）
```

换模型名、换 Key、换账号都没用。用户自然地认为是"模型名写错了"。

### 根因

本适配器的请求地址是 **`base_url + /chat/completions`**。
`/api/v3/responses` 是火山方舟的**另一套协议**（Responses API），不属于 OpenAI 兼容面。
于是请求打到了：

```
https://ark.cn-beijing.volces.com/api/v3/responses/chat/completions
                                              ^^^^^^^^^^^^^^^^^^^^^^^^^^ 不存在的路径
```

**方舟的网关对不存在的路径返回的是 `404 model not found`**，
而不是"路径不存在" —— 于是一个**拼错地址**的问题被伪装成了**模型名错**。

同一批真实数据里还发现一条 `https://ai.zyyun.xyz"/v1`（多了一个引号），
也是同一个病：地址不合法，但报错指向别处。

### 为什么难发现

- **上游的错误信息本身指向了错误的方向**。用户的所有修改动作（换模型名 / 换 Key）
  都在错误的方向上，怎么试都不会好。
- 后端其实已经把 `endpoint` 放进了错误 `detail`，但**界面上只显示了 `message`** ——
  信息就在系统里，只是没被送到用户眼前。
- 不同厂商对"路径不存在"的表达完全不同：有的返回 404 带 HTML，有的返回 404 + 模型不存在。

### 修复（三道防线，缺一不可）

1. **前端**：提交前削掉已知的接口结尾（`/chat/completions`、`/responses`、`/messages`…），
   并把"我帮你改成了什么"告诉用户（`providers.js::normalizeBaseUrl`）。
2. **后端**：构造适配器时直接拒绝这种 `base_url`，报错里带
   `suggestion`（应该填成什么）、`why`（为什么会错）、`examples`（正确写法）。
   ★ 检查放在 `OpenAICompatibleProvider` 而不是基类：Anthropic 适配器自己会判断
   用户是否已把 `/v1/messages` 填全，基类加检查会**误拦合法用法**。
3. **界面**：连通性测试失败时弹窗列出
   「实际请求地址 / 上游 HTTP 状态 / 上游原话 / 原始响应体」+ 按状态码的排查顺序。

### 回归测试

- `tests/test_llm.py::test_base_url_with_endpoint_path_is_rejected`（5 组真实错地址）
- `tests/test_llm.py::test_base_url_without_endpoint_path_is_accepted`（7 组合法地址，防误拦）
- `tests/test_llm.py::test_anthropic_base_url_may_include_full_path`（防误拦 Anthropic）
- `scripts/ui_probe.py` 的「10.5.BaseURL」四项（前端预设 + 自动削掉 + 提示文案）

---

## 17. ★ 删消息之后忘记重算统计，界面上出现"算不回来"的数字

### 现象

编辑 / 撤回会连带删除后面的消息，但 `narrative_sessions.message_count` 与
`total_tokens` 还是删除前的值 —— 界面于是显示「共 2 条消息 · 累计 12000 token」。

### 根因

统计字段是**增量维护**的（发消息时 `+1`、`+= usage.total_tokens`），
删除路径上没有对应的减量逻辑。而 `total_tokens` 累加的是厂商返回的真实用量
（含输入 token），删掉几条消息后**根本无法精确还原**。

### 修复

删除后调用 `sessions._recalculate_stats()`：按**剩余消息的 `token_count` 之和**重算。
代价是数字性质从"真实用量"变成"估算值"——这一点在代码注释里如实标注，
因为"给一个能和消息列表对上的估算值"比"留一个算不回来的旧总数"更有用。

### 为什么难发现

- 只有"删过消息的会话"才暴露，新建会话永远正常。
- 接口测试如果只断言"删了几条"、不看统计字段，就完全测不出来。

### 回归测试

- `tests/test_narrative.py::test_edit_recalculates_session_stats`
- `scripts/smoke_test.py` 步骤 11.6 的同名断言

---

## 18. 变量名遮蔽：收尾核对"永久失败"，但每一步看起来都对

### 现象

`smoke_test.py` 跑完全绿，只有收尾那句
「★ 直连数据库：行数与测试前完全一致」永远是 `[!!]`，
而它打印出来的前后两个字典**一模一样**。

### 根因

脚本第 0 步把基线存在 `before`：

```python
before = print_counts("测试前")     # 第 0 步
```

我在后面新增的「消息级操作」那段里随手写了：

```python
before = client.get(...).json()["data"]   # ← 把基线覆盖成了会话详情！
```

于是收尾比较的是 `dict == 会话详情`，恒为假。
**打印出来的两个字典当然一样** —— 因为打印的是 after 和另一个局部变量。

### 修复

基线改名为 `baseline_counts`，并写注释说明"这个名字必须唯一"。

### 为什么难发现

- 断言失败信息里只有一句"行数不一致"，看不到真实的比较对象。
- 单独跑 `print_counts` 一切正常（独立运行时没被覆盖过）。
- 这属于"检查脚本自己的 bug"（见第 7.4 条那类），
  第一反应应该是**先怀疑检查脚本**，而不是怀疑被测系统。

### 顺带记下的两条

- **临时测试账号要自己删**：`smoke_test.py` 步骤 13 只删它自己创建的三个账号，
  不按前缀批量清理（那是 `cleanup_demo_data.py` 的职责）。
  新增步骤里造了账号却忘了删，收尾核对必然失败。
- **`cleanup_demo_data.py` 的前缀清单要同步更新**（本轮补了 `msg_` / `dbg_`）。

---

## 19. ★ 列表查询把大 JSON 列读出来排序 → `Out of sort memory`（1038）

### 现象

用户导入一本条目很多的世界书之后，「世界书」页整页变成一片红：

```
OperationalError: (pymysql.err.OperationalError)
(1038, 'Out of sort memory, consider increasing server sort buffer size')
[SQL: SELECT world_books.id, ..., world_books.entries, ...
      FROM world_books WHERE world_books.user_id = %s
      ORDER BY world_books.updated_at DESC, world_books.id DESC LIMIT ...]
```

**注意这条 SQL 里 `entries` 和 `ORDER BY` 同时出现了** —— 这就是病根。

### 根因

服务层用的是 `select(WorldBook)`（取整个实体），而 `entries` 是一列 **JSON**。
MySQL 排序时要为每一行在 sort buffer 里放一份数据，本项目用的是**默认的 256KB**：

```sql
SELECT @@sort_buffer_size;   -- 262144（256KB）
SELECT @@max_sort_length;    -- 1024
```

一本世界书的 `entries` 很容易到几百 KB，于是排序缓冲直接溢出。
（官方提示"consider increasing server sort buffer size"会把人往
"去调数据库参数"的方向带 —— 但那只是掩盖问题。）

### 为什么难发现

- **只有数据量够大才触发**。实测：10 条 × 5000 字符（约 150KB）**不会**报错，
  20 条 × 15000 字符（约 880KB）才稳定复现。
  也就是说小数据的手工测试、以及任何"用两条假数据跑一遍"的测试都发现不了。
- 报错信息里的 SQL 把问题指得很清楚，但它是 500 的原始异常，
  界面上只是一片红字，用户完全不知道跟"世界书太大"有关。

### 修复

**让排序那一步绝不带 `entries`**，分两步走（`world_book_service.list_book_briefs`）：

1. 只 `SELECT` 标量列 + `ORDER BY updated_at, id` + 分页 —— 每行很小，排序缓冲再也不会爆；
2. 用第 1 步拿到的 id 列表**单独取 `entries` 一列**，在 Python 里数条目数。

顺带把列表接口彻底改成走 `WorldBookBrief`（本来就不返回正文），
并保留 `list_books()` 给真正需要整本实体的地方（脚本、批量导出）。

> 试过但**放弃**的写法：用 `JSON_LENGTH(json_extract(entries, '$[*] ? (@.enabled == false)'))`
> 在 SQL 里直接算启用条数。MySQL 8.0.28 报
> `3143 Invalid JSON path expression`（这个过滤语法在部分版本上不支持）。
> 在 Python 里数虽然多读一列，但**跨版本稳**、逻辑也看得见。

### 回归测试

- `tests/test_world_books.py::test_list_works_with_a_large_world_book`
  （★ 必须造 ~900KB 的数据才有效；小数据测不出来 —— 注释里写明了原因）
- `tests/test_world_books.py::test_detail_still_returns_full_entries_for_a_large_book`
  （反向断言：修列表不能把详情页的全文弄丢）
- `scripts/ui_probe.py` 的「世界书页渲染」一项

---

## 20. ★★ 委托监听器在"视图自己重渲染自己"时叠加 → 点一次按钮弹出 N 个一样的窗

### 现象

用户反馈：在「模型配置」页点「模型」按钮，会**同时弹出好几个完全一样的弹窗**
（截图里有 3 个）。点一次 → 弹 N 个，N 随操作次数增长。

### 根因

`#view` 是常驻元素，切页只替换它的内部内容。视图把委托监听器挂在它上面：

```js
root.addEventListener('click', handler, { signal });   // 每次渲染都执行一次
```

而**这个视图会调用自己**（"测试 / 探测 / 删除 / 保存"完都刷新列表）：

```js
await renderProviderList(root, ...);   // 旧代码里这里重新绑了一次监听器
```

于是监听器 1 个 → 2 个 → 4 个…… 一次点击被分发 N 次，每次各弹一个窗。

**这条和第 14 条（少了 `data-act`）是"同一类问题的不同形态"**：
都是"挂在常驻元素上的委托监听器"惹的祸，但触发条件完全不同 ——
第 14 条是**切页残留**（路由级 AbortController 能解决），
这一条是**同页自我重渲染**（路由级信号不起作用，因为压根没切页）。

### 为什么难发现

- 第一次进页面时完全正常（只有 1 个监听器）。**必须先点一次"测试"或"删除"**，
  让视图自我刷新一次，第二次点击才会弹 2 个 —— 手工测试很容易漏掉这个顺序。
- 探针的"数弹窗个数"这条断言才是唯一能稳定抓住它的手段：
  实测修复前 `点一次 → 弹窗=2`，再操作一次 `→ 弹窗=4`。

### 修复（两层，都要有）

1. **把"绑监听器"和"刷新列表"彻底拆开**（`providers.js` 的
   `attachProviderListeners()` 与 `renderProviderList()`）：
   刷新只重画 DOM，**绝不重新绑监听器**。
2. **给刷新加并发闸门**（`refreshing` 标志）：同一时刻只允许一次刷新在跑。
   books.js / cards.js 用 `eventsBound` 守卫做同样的事。

> 探针里新增的断言：先在页面里连点两次「测试」（制造自我重渲染），
> 再点「模型」，断言**弹窗数必须为 1**。

### 回归测试

- `scripts/ui_probe.py` 的「10.6.弹窗叠加」三项
  （点一次只弹一个 / 再点一次仍然一个 / 反复重渲染之后仍然一个）

---

## 21. ★★ 浏览器缓存导致 ES Module"版本混用"→ 控制台一片空白

### 现象

用户打开可视化控制台：**整页空白**，没有报错弹窗、没有登录页。
服务端日志显示一切正常：

```
GET /console/                 304
GET /console/js/app.js        304
GET /console/js/views/chat.js 304
GET /console/js/views/*.js    304     ← 全是 304，说明用的是浏览器缓存
```

**而 AI 自己用无缓存的新浏览器 profile 测，怎么测都是好的** —— 这就是它能溜过去的原因。

### 根因

前端是**原生 ES Module**，浏览器按 URL 长期缓存每个 `.js`。
只要改了其中一部分文件，用户浏览器里就会出现**新旧版本混用**：

```
新的 web/js/views/chat.js   →  import { freshViewSignal } from '../ui.js'
旧的（缓存的）web/js/ui.js  →  没有导出 freshViewSignal
```

ES Module 的规则是：**import 一个目标模块里不存在的导出 = 语法级错误，
整个模块图加载失败**。表现就是：

- 页面一片空白（连 `renderAuth` 都没跑到）
- 控制台只有一行不显眼的红字（用户通常不会去看 F12）
- 刷新多少次都一样 —— 因为缓存还在，混用状态不变

### 为什么难发现

- **开发者的浏览器是无缓存的**（每次探针都用全新的 `--user-data-dir`），
  所以本地怎么测都正常；只有**长期使用同一个浏览器的用户**会中招。
- 服务端日志一切 200/304，看起来"服务没问题"，容易往"服务没起来"的方向排查（我一开始就是这么误判的）。
- 触发条件是"改了前端的一部分文件"，而不是某个具体的代码 bug。

### ★ 第一版修复是错的，别再走一遍

第一版修复只加了 `Cache-Control: no-cache, must-revalidate`，理由看起来很正当：
"下次刷新浏览器就会带 ETag 回源校验，文件变了自然拿新版"。

**结果用户按了 `Ctrl + F5` 依然白屏**，而且服务日志里照样是满屏 `304`：

```
GET /console/js/chat.js   304   ← 浏览器宁可复用旧副本，也不去下载新的
GET /console/js/ui.js     304
Uncaught SyntaxError: ... does not provide an export named 'freshViewSignal'
```

同一个错误一个字都没变。**教训：对 ES Module 来说，"指望浏览器自己重新校验"
不是可靠的机制**；只要 URL 不变，就存在"用户机器上有一个说不清的旧副本"的可能，
而你没有任何手段从服务端把它清掉。

### 修复（两层，缺一不可）

1. **模块 URL 带版本号（importmap，根治）**

   `web/index.html` 里放一张 importmap，把**裸名**映射到带版本号的真实 URL；
   后端渲染入口页时替换掉 `__WEB_VERSION__`：

   ```html
   <script type="importmap">
   { "imports": { "hne/ui": "/console/js/ui.js?v=__WEB_VERSION__", ... } }
   </script>
   ```

   前端模块之间也随之从相对导入改成裸名导入：

   ```js
   import { modal } from 'hne/ui';      // 之前是 '../ui.js'
   ```

   版本号 = **所有前端文件的 mtime + size** 的 sha256 前 12 位
   （`app/main.py::_frontend_version`，带 2 秒缓存）。

   - 版本变了 → 所有模块 URL 一起变 → 浏览器**没有旧副本可复用**，必须重新下载；
   - 版本没变（只改后端 Python）→ URL 不变 → 照旧吃缓存，不牺牲性能。

   > 为什么不用内容哈希？那要把每个文件读一遍才算得出来；
   > mtime 只在文件真的被改过时才变，语义刚好够用，一次 `os.scandir` 就够。
   > 为什么不用"文件名带哈希"（webpack 那套）？那需要构建步骤，
   > 与本项目"零构建、改完刷新即生效"的取舍冲突；查询串版本号效果一样但不需要打包器。

2. **`boot.js` 自愈（防"入口页自己被缓存"）**

   `index.html` 自己也可能被缓存住，那就带着**旧版本号**的 importmap 了。
   `web/js/boot.js`（排在 `app.js` 之前）用 `fetch('/console/', {cache:'no-store'})`
   问一次真实版本（服务器通过 `X-HNE-Web-Version` 响应头回答）：

   - 对不上 → `location.replace('/console/?v=<新版本>')` 自动重载，用户什么都不用做；
   - 重载也没救回来 → 显示一条红色提示条（`.boot-stale`），**而不是让用户对着白屏发呆**。

### 怎么防它再回来

`tests/test_console.py` 新增 8 项回归测试，把机制本身钉死：

| 断言 | 防的是什么 |
|---|---|
| 入口页占位符必须被替换、必须是 `no-store` | 版本号没渲染出来 → 全盘失效 |
| 各模块 URL 必须带**同一个**版本号 | 漏一个就会出现跨版本混用（原样复发） |
| `web/js/**` 里每个模块都要登记进 importmap | 新增模块忘了登记 → 运行时白屏 |
| 模块之间**禁止**相对导入（`'../ui.js'`） | 相对 URL 不带版本号，等于没修 |
| 改前端文件后版本号必须变 | 版本不变 = URL 不变 = 缓存击穿失效 |
| `boot.js` 的关键行为不能被删 | 防止被当成"没用的文件"清理掉 |

探针（`scripts/ui_probe.py`）也用**页面里的裸名** `import('hne/ui')` 走一遍，
顺带验证 importmap 真的能解析这些名字。

### 给用户的自救动作

正常情况**不需要**任何自救 —— 改完前端刷新页面即可。
万一还遇到白屏：`Ctrl + F5`，或 F12 → Application → Storage → Clear site data。

### ★ 附带发现：探针里一个"看起来通过其实没测到"的竞态

改完缓存机制后重跑探针，第 6 节「★ 编辑后的内容出现在历史里」开始**间歇性失败**
（同一份代码，三次里挂一次）。服务端日志显示 `PATCH …/messages/3386` 返回 200、
随后的 `GET` 也正常 —— 接口没问题，是**断言时序**有问题：

```
编辑的流程：PATCH → openSession 重渲染（危险窗口）→ 重新生成
断言等的是："最后一条气泡不在 pending 且有内容"
```

而"危险窗口"里，最后一条消息**还是编辑前那条旧回复**（既不 pending、也有内容），
于是断言可能抢在重渲染之前取样，**看到一个看起来通过、其实还没更新的状态**。

修法：断言改成等**编辑后的文字真的出现在历史里**（等一个必然会发生的事件，
而不是等一个"现在就已经成立"的条件）。连跑三次全过。

> 顺带修掉了同一窗口里**真实用户也会踩到**的问题：那 100ms 里旧消息还挂在界面上，
> 用户点它上面的「撤回 / 重新生成」会按**旧的 id** 去删数据，编辑的内容会被抹掉。
> 现在整个窗口用 `state.streaming` 罩住（见 `chat.js::openEditDialog` 的注释）。

---

## 22. ★★ "只绑一次"的守卫认错了对象：监听器被绑在一个**已经 abort 的信号**上

### 现象

用户反馈：

> 点击角色卡里的「公共卡库」或「全部可见」之后（可能还包括其他按钮，
> 点了「导入 PNG」之后也发现动不了其他键了），这个页面的按钮就失效了（点击无反应）。

注意他描述的范围：**页面内的按钮**（详情 / 导出 / 编辑 / 复制 / 删除 / 导入 / 新建）
全都没反应，而**顶栏的导航还能点**。这个"哪些能点、哪些不能点"的分界线极其关键 ——
它说明坏掉的不是渲染、也不是接口，而是**挂在 `#view` 上的那一层事件委托**。

### 根因

`web/js/views/cards.js` 里"监听器只绑一次"的守卫长这样：

```js
async function renderCardList(root, opts = {}) {
  const signal = opts.signal;          // ← 调用方传进来的信号
  ...
  while (true) {
    if (eventsBound && !signal.aborted) break;   // ← 判据用的是它
    eventsBound = true;
    ...
    root.addEventListener('click', handler, { signal });   // ← 绑的也是它
    break;
  }
}
```

问题出在**"判据"和"绑定对象"虽然不是同一个东西，但共享了同一个可能失效的值"**：

1. 筛选按钮那条路径调用时**根本没传 signal**：`renderCardList(root, { scope })`
   → `signal === undefined`，于是监听器被绑成**无信号**（永远不会被摘掉）；
2. 而视图"自己重渲染自己"时传进来的可能是**已经 abort 的旧信号**
   → `signal.aborted === true`，守卫判断"还没绑过"，于是**用这个死信号再绑一次**。

`addEventListener(..., { signal: 已经abort的信号 })` 的行为是：
**监听器根本不会被注册**（或者立刻被移除）。于是：

- 委托监听器**一诞生就是死的**；
- `eventsBound` 已经被置为 `true`，后续刷新**再也不会重绑**；
- 结果就是：所有 `data-act` 按钮永远无反应，而顶栏（另一套监听器）完全正常。

### 为什么难发现

- **全程没有任何报错**：不抛异常、不发请求、控制台干干净净。
  唯一的线索是服务端日志里"点按钮时一条请求都没有"。
- 表现是"点着没反应"，人的第一反应是"按钮坏了/CSS 挡住了/接口挂了"，
  而不是"事件监听器是个死的"。
- 它**只在特定路径之后出现**：初次进入页面是好的（那次带了正确的信号），
  必须先点一次筛选（或做一次会触发重渲染的操作）才会掉进去。
- 代码里那句 `if (eventsBound && !signal.aborted) break;` **看起来完全正确** ——
  这正是它危险的地方：一个看起来对、实际判错了对象的守卫。

### 修复（把判据和对象统一）

```js
// 一律绑定"当前这一批"的信号，绝不用调用方传来的旧信号
const bindSignal = activeSignal?.signal ?? opts.signal;
const signal = bindSignal;                       // 内部也统一用它，避免再出现"漏传"

while (true) {
  if (eventsBound && !bindSignal?.aborted) break;
  if (bindSignal?.aborted) break;                // 死了就别绑（绑了也是死的）
  eventsBound = true;
  ...
  root.addEventListener('click', handler, { signal: bindSignal });
  break;
}
```

外加两条保险：

- 拿不到任何可用信号时 `console.error` 明确报出来 ——
  **绝不静默绑一个永远不会触发的监听器**（那正是这次吃亏的方式）；
- 筛选按钮那条路径也显式传 `signal: activeSignal.signal`。

### 回归测试（`scripts/ui_probe.py` 第 10.7 节）

这条 bug **静态检查看不出来**，必须在真实浏览器里点。探针做的三件事：

1. 依次切「公共卡库 / 全部可见 / 我的卡库」，**每次切完都点一次卡片上的「详情」**，
   断言弹窗真的打开；
2. 离开角色卡页后，断言请求日志不再增长（没有残留监听器泄漏到别的页面）；
3. ★ 顺带发现探针自己的一个盲区：原来只有一个账号，
   而 `scope=public` 的语义是"**别人**公开的卡"，所以公共卡库永远是空态 ——
   **bug 只有在列表里确实有卡片时才暴露**。现在探针会额外建一个账号
   （`ui_probe_other`）放一张公开卡，公共卡库才真的有东西可点。

> 还有一个同类坑：探针起无头浏览器时**没指定窗口尺寸**，默认只有 800px 宽，
> 正好落进 CSS 里 `max-width: 900px` 的移动端断点 ——
> 于是"两栏对话区 / 可拖动调节大小"这类只在桌面生效的功能**根本测不到**
> （第一次加拖拽断言时就是这么失败的）。现在固定成 `--window-size=1360,1000`。

---

## 23. ★★ 把规范字段"按字面猜语义"，结果整套机制理解错了

### 现象

做「酒馆式提示词预设」时，我先按字段名推断：

- `injection_position = 0` → "按顺序装配"
- `injection_position = 1` → "按深度注入历史"
- 又看到酒馆给**很多**块都填了 `injection_depth`，于是推断
  "没进 `prompt_order` 顺序表的块，都是靠深度注入生效的"。

基于这个推断写完了装配逻辑与测试，测试还全绿 —— 因为测试用的是**我自己编的
示例预设**，那份数据是照着我的（错误）理解构造的。

### 根因

**拿真实文件一比对，全错了**：那份 21 块的真实预设里，
**所有块的 `injection_position` 都是 0**，包括 `main`、`nsfw`、
以及 6 个 UUID 规则块。它们的 `role` 是 user / assistant，
含义是「**以 user 或 assistant 的语气写进系统提示词**」，
根本不是"插进对话历史"。

也就是说：我差一点交付一套"看起来跑通了、但和酒馆真实行为不一致"的实现 ——
而用户拿它去导真预设时，破甲的位置会全错，**且不会报任何错**。

### 为什么难发现

- 字段名有暗示性（`injection_position` / `injection_depth`），
  很容易"读懂"成自己以为的意思，从而**不去查证**。
- 自造样例 + 自写断言 = 自证循环：测试全绿反映的是"我理解的一致性"，
  不是"与真实规范的兼容性"。
- 错误的表现是"模型不按预期表现"，而不是崩溃 —— 用户很难归因到字段语义。

### 修复

1. 判据改成**只看 `injection_position`**：
   `1` 才是深度注入；`0` 一律归入系统提示词（哪怕 role 是 user/assistant）。
2. **拿用户的真实文件当断言依据**：`tests/test_presets.py` 里加了一条
   "真实文件存在才跑"的用例，直接断言
   `[b for b in config.blocks if b.depth_injected] == []` ——
   把"真实文件里没有一个块是深度注入"这件事钉死在测试里。
3. 注释里写清**我读错过一次**，避免下一个人（或下一轮的我）再按字面猜。

> 方法论：**涉及外部规范的字段，第一件事是把真实样例摊开数一遍**，
> 而不是读字段名。这一条值得单独记 —— 它比这个 bug 本身更值钱。

---

## 24. ★ 只 `flush()` 不 `commit()`：写进去的数据"下一个请求就消失"

### 现象

预设导入接口 `POST /prompt-presets/import` 返回 **201**，响应体里
`id=37`、块数量都对。紧接着 `GET /prompt-presets/37/export` 却返回
**404 提示词预设不存在**。列表也查不到。

### 根因

本项目的数据库依赖（`app/db/mysql.py::get_db`）是**不自动提交**的
（`autocommit=False`，事务由业务代码显式控制，这是刻意的设计）。
服务层写完数据只调了 `db.flush()`：

- `flush()` 只把 SQL 发给数据库、拿到自增主键，**事务没有提交**；
- 请求结束时连接被回收 → **回滚**；
- 于是"创建成功"的响应是真的，"数据不存在"也是真的。

### 为什么难发现

- **写接口自己测不出问题**：它拿到的是同一个事务里的对象，
  读起来一切正常（甚至 `id` 都对）。
- 只有"**跨请求再读一次**"才会暴露 —— 单测如果只在同一个会话里断言，永远绿。
- 项目里其它服务（角色卡 / 世界书 / 模型配置）都写了 `commit()`，
  漏掉一处不会有任何提示。

### 修复与防回归

- 所有写路径显式 `db.commit()`，并在注释里写清"接口层不自动提交"。
- 需要拿到自增 id 后再做同事务更新（例如"设为全局默认"要排除自己）时，
  顺序是 **`db.flush()` → 同事务更新 → `db.commit()`**，不要 commit 两次。
- `tests/test_prompt_presets.py` 的用例全部是
  "写 → **换一个请求**读"，天然能抓到这类问题。

---

## 25. 固定路径被路径参数抢先匹配（`/preview` 被 `/{preset_id}` 吃掉）

### 现象

`GET /api/v1/prompt-presets/meta` 正常，但
`GET /api/v1/prompt-presets/preview?preset_id=1` 返回 **404 提示词预设不存在**。
看起来像是预览功能的业务逻辑报的错。

### 根因

FastAPI 按**注册顺序**匹配路由。`/{preset_id}` 先注册的话，
`/preview` 也会被它匹配上，然后拿 `"preview"` 去解析成 `int` 失败 ——
**它不会回头去找后面的固定路径路由**，而是直接给出 404/422。

### 修复

把所有固定路径（`/meta`、`/preview`）**写在 `/{preset_id}` 之前**，
并在文件顶部写明这条规则。本项目的 `router.py` 注释里其实早就写了同样的提醒
（`providers` 模块踩过），这次是第二次踩 —— 所以在
`prompt_presets.py` 里把"为什么"也写进注释，而不只是写"要这么做"。

---

## 26. ★★ 深度注入必须"裁剪之后才插回"

### 现象（差点踩到，靠想清楚顺序避开了）

预设里的"深度注入"块要插进对话历史。最直觉的写法是**拼装时就插进去** ——
结果是：上下文裁剪器从最早的消息开始丢，深度块被当成"旧消息"丢掉，
破甲静默失效；或者它不参与裁剪计数，把输入预算挤爆。

两种结果都**不报错**，只是模型行为不对。

### 根因

`prepare_context` 的裁剪策略是"**从最早的消息开始丢**，
并把丢掉的压成滚动摘要"。而深度注入的块：
- 必须在**裁剪之后**才能确定"倒数第 N 条"到底是哪一条；
- 如果参与裁剪，它会被优先丢掉（它在最前面）。

### 修复

```
prompt_builder  只产出 (深度, ChatMessage) 列表，不插进 messages
      ↓
context_manager 先按预算裁剪历史
      ↓
              再按"倒数第 depth 条之前"插回（下标 = 总条数 - depth，夹到合法范围）
```
实现放在 `context_manager.inject_depth_messages`，并单独测试：
`depth=1` 插到最后一条之前、`depth=0` 追加到最后、
深度超过历史长度时插到最前面（而不是越界或丢失）。

---

## 27. 同一个接口"列表返回数组、详情返回对象"，前端写成 `.items` 静默变空

### 现象

预设导入成功（201、块数量正确），但预设页**一张卡片都不显示**，
控制台没有任何报错，列表接口在请求日志里也是 200。

### 根因

前端写的是 `(await api.get('/prompt-presets')).items || []` ——
假设列表接口返回的是分页壳 `{items, total}`。
但预设列表接口返回的是**纯数组**（预设数量天然很少，不需要分页）。
于是 `.items` 是 `undefined`，`|| []` 把错误**吃掉了**，界面显示"还没有预设"。

### 为什么难发现

- `|| []` 让"接口形状不对"退化成了"列表为空"这个**看起来完全正常**的状态；
- 后端日志一切 200，前端也不报错 —— 三方都"正常"，只有用户看到空列表。

### 修复

前端统一做形状归一：`const items = Array.isArray(res) ? res : res?.items || []`。
并且在注释里写明"这个接口返回的是数组，不是分页壳"。

> 值得单独记的一条：**`x || []` / `x || {}` 这类兜底会把契约错误伪装成空数据**。
> 兜底可以留（避免整页崩掉），但要在注释里点明"这里为什么可能是两种形状"。

---

## 28. 预览与实际发出去的东西，必须走同一条路径

### 现象

「查看提示词」显示来源是"引擎按角色卡的人设字段自动拼装"，
但用户明明已经给这个会话绑定了预设 —— 而真正发消息时用的是预设。
**预览和实际不一致**，用户会拿着预览去排查一个不存在的问题。

### 根因

会话详情里构建预览的 `_prompt_preview()` 是**另一条调用路径**，
它调用 `engine.build_session_prompt()` 时**没传 preset**；
而真正发消息的 `prepare_turn()` 传了。
两条路径的参数一旦不同步，预览就会撒谎。

### 修复

- 两处都传 preset，并且**用户名（`{{user}}` 宏）也共用同一个函数**
  （`engine.current_user_name`）—— 口径不同的地方越少越好。
- `prompt_preset_service.preview()` 也复用 `engine.load_card_and_book` /
  `world_book_scanner.scan` / `prompt_builder.build_prompt`，
  不再自己拼一套。
- 探针里加断言：绑定预设后，**「查看提示词」里必须出现"预设装配明细"**
  （第 10.8 节），这条断言就是专门守"预览撒谎"的。

> 这个坑在本项目**出现过两次**（世界书注入、预设装配）。
> 结论：凡是"预览/试算/预演"类功能，都必须调用与真实执行**同一个函数**，
> 而不是"照着抄一遍逻辑"。

---

## 29. ★★ 少一个右花括号 → 全站前端模块加载失败（而 `node --check` 说"没问题"）

### 现象

加完"HTML 开场白渲染"与"文件拖放"之后，**整个控制台又白屏了**：
控制台三条红字

```
SyntaxError: Unexpected token 'export'
```

### 根因

我往 `web/js/ui.js` 里插入新段落时，把 `spinner()` 的**闭合花括号吃掉了**：

```js
export function spinner(text = '处理中') {
  return `<span class="spin"></span> ${esc(text)}`;
/**            ← 少了一个 } ！于是 import/export 跑进了函数体内部
 * 让按钮进入"加载中"状态…
 */
export function buttonLoading(btn, text) { … }
```

函数体里出现 `export` → **整个模块解析失败** → 依赖它的所有模块一起挂。
和之前那次白屏不同：这次不是缓存问题，是**真的语法错误**。

### ★ 为什么 `node --check` 没拦住（这才是最值钱的一条）

我改完立刻跑了 `node --check web/js/ui.js`，**退出码 0**。

原因：`node --check` 默认按 **CommonJS** 解析。ES Module 里的 `import` / `export`
在 CJS 下本来就是"意外的 token"，但 `--check` 对这类情况**不报错就直接通过**；
换句话说：

- 语法**对**的文件 → 它可能报错（把 `export` 当非法 token）→ **假阳性**
- 语法**错**的文件 → 它照样通过 → **假阴性**

所以我之前"`node --check` 全绿"的那几次，其实什么也没保证。
真正能验证的只有一条路：**按 ES Module 真的加载一次**：

```bash
node --input-type=module -e "import('./web/js/ui.js').then(()=>console.log('ok')).catch(e=>console.log(e.message))"
```

本项目里等价物是探针第 1 节的"所有前端模块可加载"守卫 ——
它是**唯一**能抓住这类错误的地方。

### 修复与两条防回归

1. 补回花括号；所有前端改动用「按 ES Module 加载一次」验证（见上）。
2. ★ **探针把"模块可加载"改成前置条件**：一旦某个模块加载失败，
   立即中止后续检查（`ModuleLoadFailure`），只报这一条并写出报告。
   为什么必须这样：模块挂了页面只是"半活"的，后面的点击会得到一串
   莫名其妙的结果（按钮找不到、文本取不到），
   **真实踩过**：这次探针一路点下去，报告里全是"按钮没反应"，
   把真正的原因（一行 SyntaxError）埋在了 20 条假失败下面。
3. 顺手加固了探针自身：`cdp.eval` 出错时返回的是 `{"__error__": …}`（dict），
   直接 `.strip()` / 做减法会让**整个探针崩掉**（前面已通过的检查连报告都写不出来）。
   现在取文本/取数字一律走 `cdp.text()` / `cdp.num()`，出错只会让**那一条**断言失败。

---

## 30. 同一个"监听器绑死"的坑，在 `books.js` 里又踩了第二次

### 现象

用户第二次反馈两件事，看起来是两个 bug，其实是同一个根因：

> 1. 在角色卡界面中点击按钮后按钮就失效了；
> 2. 世界书点编辑后再回到世界书界面就**一直卡在"加载中"**。

### 根因

第 22 条修的是 `cards.js`；**`books.js` 里有同样的一行没改**：

```js
root.addEventListener('click', handler, { signal });   // signal 是调用方传进来的
while (true) { if (eventsBound && !signal.aborted) break; … }   // 判据也是它
```

于是：传进来的是**已经 abort 的旧信号**时（"自己重渲染自己"那条路径），
监听器被绑成"一诞生就是死的"，而 `eventsBound` 已置 true，
**再也不会重绑** → 本页所有按钮永久失效。

"一直卡在加载中"是它的另一种表现：`renderBookList` 里
`mount("加载中…")` 之后的请求回来时，`signal.aborted` 为真 → 直接 `return`，
页面就永远停在那个"加载中"占位上了。

### 修复（这次连"判断依据"一起换掉）

不再用布尔标志 `eventsBound` 判断"绑过没有"，而是**记住绑的是哪个信号**：

```js
let boundSignal = null;                       // 记住"绑过哪一批"
const bindSignal = activeSignal?.signal ?? signal;   // 一律用当前批次
if (!bindSignal) { console.error(...); return; }
while (true) {
  if (boundSignal === bindSignal) break;      // 这一批已经绑过
  if (bindSignal.aborted) break;              // 死信号不绑
  boundSignal = bindSignal;
  …
  root.addEventListener('click', handler, { signal: bindSignal });
}
```

`cards.js` 也一起改成同样的写法 —— 两处形状一致，以后才不会有"改了一处漏一处"。

### 回归测试（探针第 10.9 节）

专门覆盖**真实的用户操作顺序**：

```
列表 → 打开编辑（弹窗）→ 保存（触发列表重渲染）→ 返回列表 → 再点一次
```
断言：保存后列表重新渲染出卡片（不是"加载中"）、再点「查看/编辑」仍能打开弹窗。
世界书与角色卡各走一遍。

> 这个 bug 已经复发两次了。教训写在最前面：
> **只要一个视图会"自己重渲染自己"，它的委托监听器就必须绑定在"当前批次"的信号上，
> 并且用同一个信号判断是否已绑。** 布尔标志不够用。

---

## 31. 「重新生成」把"要删的那条回复"当成了"要问的那句话"

### 现象
点「重新生成」或"编辑后确认"，**AI 完全没有输出**，只有一条红色错误一闪而过，
紧接着气泡被列表重载清掉，看起来像"什么都没有发生"。

### 根因
`/regenerate` 的实现里只有**一个** id 变量，却承担了两种含义：

| 含义 | 应该是什么 |
|---|---|
| 从哪条消息开始删 | 用户点的那条 **assistant** 回复 |
| 拿哪句话去问模型 | 那条回复**之前**的 **user** 消息 |

老代码把 assistant 回复的 id 同时当成两者：

```python
delete_messages_from(..., after_message_id=user_message.id, inclusive=True)  # 删掉了它
engine.stream_reply(stream_db, session, user_message)                        # 又拿它当提问
```

于是模型收到的是**角色自己刚说过、而且已经从事务里删掉的那条消息**。
前端"编辑后确认"那条路更直接：它把 **user 的 id** 传给一个"只接受 assistant"的接口，
必然 400。

### 修复
1. `_prepare()` 返回**两个**值：`(提问锚点 user 消息 id, 删除起点 id)`；
   `_sse_stream_response` 新增 `delete_from_id`，与 `user_message_id` 彻底分开。
2. 新增 `sessions.find_previous_user_message()`（回退到那条回复之前的用户发言）。
3. 前端「编辑后确认」不再传 `message_id`，让后端按"最后一条用户消息"自己找锚点
   （PATCH 已经把后面的内容都删了，它必然就是最后一条）。
4. 失败时**额外弹一次 toast**：失败气泡会在 `silentReload` 里被重建掉，
   只留内联红字就等于"什么都没提示"。

### 回归测试
`tests/test_narrative.py::test_regenerate_prompt_anchors_on_previous_user_message`
断言的是**真正发出去的提示词**：最后一条用户发言必须等于那条用户消息，
且被替换掉的回复内容不得出现在提示词里。

> 教训：**一个变量承担两种语义**是这类 bug 的温床。名字里带 `_id` 的时候，
> 先问一句"这个 id 指的是谁"。

---

## 32. 备选开场白用换行拼接 → 1 条变成 67 条 → 保存 422

### 现象
编辑角色卡点「保存修改」，报 `请求参数校验失败 / alternate_greetings: 最多 20 个，当前 67 个`。
用户完全看不出为什么 —— 他明明只写了**一条**开场白。

### 根因
前端把备选开场白当成"一行一条"：

```js
const altGreetings = (existing?.alternate_greetings || []).join('\n');  // 显示
body.alternate_greetings = v.alternate_greetings.split('\n')...         // 保存
```

但**一条开场白本身就可能有很多行**（分段正文、状态栏、HTML 卡片都是多行的）。
那张卡其实只有 1 条 3107 字的备选开场白，含 66 个换行 → 拆成 67 条 → 撞上限。

### 修复
- 前端改成**一条一页的翻页编辑器**（上一条/下一条/新增/复制/删除），
  **永远不按换行拆分**；翻页前先把当前页写回数组（否则改一半翻页 = 丢改动）。
- 后端上限 `MAX_GREETING_COUNT` 20 → 100（它当初是配合"一行一条"定的）。

> 教训：**"用分隔符拼接一组富文本"几乎总是错的**。只要单项内容可能含分隔符，
> 就必须用数组结构（或分页 UI）表达，而不是 join/split。

---

## 33. 内置守卫预设"替代"了内置装配，把角色人设挤没了

### 现象
加完"内置守卫规则（身份认知 / 不跑偏 / 输出长度）"之后测试立刻红了：
系统提示词里**只剩几段规则**，角色卡的简介 / 性格 / 场景 / 世界书全都不见了。

### 根因
`build_prompt` 原本的分支判据是 `if preset is not None`。为了"没绑预设也要有守卫"，
我把它改成了 `if preset is not None or preset_config is not None` ——
于是"没绑预设"的用户走进了**预设装配**路径，而守卫预设里根本没有
`charDescription / charPersonality / scenario / worldInfo` 这些**标记块**，
那些内容自然一个都没注入。

### 修复
把两者的关系从"二选一"改成**叠加**：

```
没绑预设：内置装配（人设 / 世界书 / 记忆，逐字节不变） + 守卫正文追加在系统提示词最后
绑了预设：用户预设的块（按它的顺序） + 守卫块接在最后（order_index 整体偏移）
```

判据也改回 `preset is not None`，`preset_config` 只在**内置装配那条路**里被读取。

> 教训：**"追加一条规则"和"换一套装配"是两件事。**
> 用一个判据同时表达两种意图，就会把不相关的功能一起换掉。
> 这次是测试先红，而不是用户先发现 —— 这就是那 500+ 条测试的价值。

---

## 34. 探针报"HTML 没渲染"，其实是它读错了弹窗

### 现象
浏览器探针 10.10 报"HTML 开场白没渲染"，但手工点开那张卡明明是好的。

### 根因
`#modal-root` 里**同时存在多个弹窗**（前一个用例没关干净）。
探针用的是：

```js
document.querySelector('#modal-root [data-rich="greeting"]')   // 文档里第一个
```

命中的是**上一张卡**的弹窗 —— 那张卡的 `greeting` 是纯文本，
于是"纯文本分支"当然没有 iframe。

### 修复
- 打开目标卡之前先 `[data-close].click()` 清场；
- 所有查询改成 `[...document.querySelectorAll(...)].pop()` 取**最后一个**弹窗；
- 断言失败时打印**结构化诊断**（`{hasPre, paneHidden, panes…}`）而不是裸 `False`
  —— 正是这个诊断一眼指出了"读到的是另一张卡"。

> 教训 1：选择器要看**"是不是当前那一个"**，`querySelector` 只给你"第一个"。
> 教训 2：**断言失败必须自带上下文**。探针自己也是一份代码，也会说谎。

---

## 35. 加了骰子插件之后，纯聊天会话"建好了却没被打开"

### 现象
浏览器探针从 108 项涨到 116 项后，骰子节 8 条全绿，但后面「纯聊天」节
有两条变红：`#chat-title` 还是上一个故事会话的标题，`#chat-sub` 显示的是
"探针角色 · 共 3 条"，而接口里**确实存在**一个新建的纯聊天会话。
（更迷惑的是紧跟着的"纯聊天能收到回复"还是绿的 —— 它等的是"最后一条消息有内容"，
而旧会话本来就有内容，于是**假通过**。）

### 根因
我在骰子节收尾把界面留在了**对话页**。下一节「纯聊天」的写法是：

```js
click('#nav [data-route="chat"]')            // 触发一次**异步**重渲染
wait_for(h1 含 '叙事会话')                    // 已经在对话页 → 立刻返回
click('#btn-pure-chat')                      // 点到的是**旧** DOM 上的按钮
click('#btn-create-pure')                    // 弹窗里的处理函数持有旧 view signal
```

点导航会 `freshViewSignal()` 出新信号并 **abort 旧的**。于是弹窗里这段：

```js
const created = await api.post('/narrative/sessions', payload);
m.close();
if (signal?.aborted) return;     // ← 就在这里静默返回了
```

**会话建好了，界面毫无反应**（不报错、不刷新、不打开）。

### 修复
- 骰子节收尾**切到插件页**再结束 —— 下一节的"点导航 + 等标题"才会等出一个**新**视图；
- 把这个理由写在探针里（注释），因为下一次调整节序的人一定会再踩。

> 教训：探针里的**节序**是一份隐式契约（"上一节把界面留在哪里"）。
> 断言除了看结果，还要看**你断言的那个 DOM 是不是这一节的 DOM**。

---

## 36. 内置目录的「添加」按钮不能按"第一个"点

### 现象
骰子插件放进内置目录（并排在第一条）之后，探针 12 节原本稳过的
"目录里的示例点「添加」后才进我的插件"行为变了：它加进去的是骰子插件，
后面的断言开始依赖"恰好加的是哪一个"。

### 根因
探针写的是 `document.querySelector('.catalog-item button[data-catalog-add]').click()`
—— **靠顺序**。目录会随版本增长，靠顺序的断言一定会被新条目搞坏。

### 修复
改成按 key 精确点：`button[data-catalog-add="authors_note"]`。

> 教训：**凡是"列表里的第一个"都要问一句"它凭什么永远是第一个"**。
> 同一类问题在别处也出现过（弹窗取 `querySelector` 而不是 `.pop()`，见第 34 条）。

---

## 37. 给 `validate_config` 加分支时，把 CSS 那一整段带走了

### 现象
加 `dice` 分支后，`validate_config('css', ...)` 走进了骰子的校验代码
（报 `NameError: MAX_TRIGGER_CHARS`），`pytest` 立刻红。

### 根因
`validate_config` 是"一串 `if kind == ...: return`，最后一段兜底当 `css`"的结构。
我用 edit 插入新分支时，锚点选在了 `# kind == "css"` 那两行上，
新分支的 `return` 之后**原来的 CSS 代码变成了不可达的死代码**（缩进/控制流错位）。

### 修复
把 CSS 分支重新放回函数体内（并在注释里写明"这是唯一剩下的分支"）。

> 教训：在"多分支 + 最后一段兜底"的函数里插分支，锚点要选**紧邻 `return` 的那一行**，
> 插完立刻跑一遍**这一层**的测试（校验层测试很快，不值得省）。

---

## 38. "旧 view signal"竞态的第二例：探针拖不动会话列表宽度

### 现象
`11.拖动大小` 两条断言**偶发**变红（十次里红一两次）：
合成 pointerdown / pointermove / pointerup 都派发了，宽度却一点没变（433 → 433），
紧接着的"双击恢复默认"也没反应。手工拖是好的。

### 根因
与第 35 条同源，但这次受害者是**合成事件**：

```js
点击「叙事会话」导航（此刻已经在该路由）→ 仍可能触发一次异步重渲染
querySelector('#chat-resize')            // ← 拿到的可能是**旧节点**
dispatchEvent(pointerdown …)             // 旧节点的监听器已随旧 view signal abort → 没人处理
```

`initChatResize` 是用 view signal 绑定的；视图一重建，旧节点上的监听器就全废了。
**dispatchEvent 不做命中测试**，所以"元素还在 DOM 里"并不代表"事件有人接"。

### 修复
拖动前**先切到别的页再切回来**（`books` → `chat`），保证这次视图是新渲染的。

> 教训（把第 35 条推广一下）：探针里凡是要给某个元素派发事件，
> 先确认那个元素属于**当前这次渲染**；"还能 querySelector 到"不等于"它还活着"。

---

## 39. 两处文本的"谁给谁看"：`display` 语义差点写反

### 现象
翻译中间件做完之后，**接口返回的数据是对的、单元测试也全绿**，
但界面在"输入侧"会把**译文**当成正文显示出来（用户写的原话被藏起来）。
是截图复核时看出来的：气泡里是那句英文译文，而用户明明打的是中文。

### 根因
两处各自"看起来对"的约定撞在一起：
- 后端：输入侧 `content` = 译文（模型看到的），`translation.text` = 用户原话，
  于是我把 `display` 标成 `"content"` —— 想表达"给人看的是 content 对应那一侧"；
- 前端：`display === 'content' ? content : translation.text` ——
  字面意思是"`display=content` 就显示 `content`"。

两边对 `display` 的读法刚好相反。测试之所以没抓到，是因为断言只看了
`display` 这个**字符串**，没看"界面到底会渲染哪一份"。

### 修复
把不变量写死成一句话，两处都按它实现：

```
content               = 模型看到的文本（输入侧=译文，输出侧=模型原文）
translation.text      = 给人看的文本（输出侧=译文，输入侧=用户原话）
display               = 界面该显示哪一份（当前两种方向都是 translation）
```

并把"给人看的那一份"顺手用到了**会话列表预览**上（否则气泡是中文、列表是外语，
界面自相矛盾）；测试里补了一条把两个方向都钉死的不变量断言。

> 教训：跨前后端的**语义字段**（`display` 这种）必须在**两边各写一遍注释**，
> 并且测试要断言"**最终渲染出什么**"，而不是只断言那个字符串等于什么。

---

## 40. `AbortController` 接力漏了一环：切页后监听器永不失效，删除弹两个窗

### 现象
用户点「插件」页的**删除**，弹出**两个**确认窗；第二个是「删除这套预设？」，
确认之后还报 `404 提示词预设不存在`（它拿**插件的 id** 去打预设接口）。
刷新页面后第一站就去插件页，则**不复现**。

### 根因
第 22 / 10 条修的是"同一个视图重复绑监听器"。而这里漏的是**另半条**：

```js
// presets.js
function nextController(current) { current?.abort(); return new AbortController(); }
listController = nextController(listController);   // ← 新控制器不在路由信号的链上
const signal = listController.signal;
withSignal(root, 'click', …);                      // ← 挂在常驻的 #view 上
```

`#view` 是**常驻**元素。一个挂在它上面的委托监听器能不能在切页后失效，
**只看它的 signal 有没有接在路由信号上**。"每次都换新控制器"看似安全，
实际上把父子链剪断了 —— 切页时 `routeAbort.abort()` 摘不掉它。
于是预设页的监听器一直活着，而预设页与插件页的卡片同为 `.cc`、
删除按钮同为 `data-act="del"`，插件页的点击被它接走。

### 修复
1. `nextController(current, parent)` 接力视图信号（两处调用都传 `activeSignal?.signal`）；
2. 加一道**与信号无关**的防线：五个列表容器各带 `data-view="<页面名>"`，
   处理函数开头 `if (!btn.closest('[data-view="…"]')) return;`
   —— 认不出归属的按钮一律不管，"在 A 页点按钮、B 页的动作被执行"结构上不可能；
3. 探针断言改成**走用户的真实路径**（先预设页 → 再插件页 → 点删除），
   要求恰好 1 个窗**且标题是「删除插件」**。

> 教训①：凡是"新建控制器/新批次"的写法，都要问一句 **"谁会 abort 它？"**。
> 教训②：**静态测试只数 `{ signal }` 的个数是假防线** —— 这里是经
> `withSignal()` 间接绑定、变量名也换了，数字对得上而 bug 照样在。
> 教训③：老断言（10.6）一直是绿的，因为它**只在本页反复点击**；
> 跨页 bug 必须复现"先过 A 页"这一步。

---

## 41. iframe 不继承父页面 CSS 变量：深色主题下永远有一块白

### 现象
用户切到深色主题后，**角色卡页永远有一整块白**，换什么主题都换不掉。
排查时用"列出所有非透明背景的元素"的脚本去筛，只筛出一堆**品牌蓝**，
真正的白块根本没出现在列表里。

### 根因
HTML 开场白渲染在 `srcdoc` iframe 里（`sandbox="allow-same-origin"`，
故意不给 `allow-scripts`）。iframe 是**一份独立文档**：
父页面的 `--surface` 之类的 CSS 变量**一个都进不去**，
而那段文档头里写死了 `body{background:#fff;color:#1f2328}`。
所以"把写死的颜色收进 CSS 变量"这个修法对它**完全无效**。

至于排查脚本为什么漏掉它：白来自**子文档**，父元素那一层是**透明**的，
而脚本只比较每个元素自己的 `backgroundColor` 亮度。
（同一个脚本还会**误报**：品牌蓝 `rgb(124,156,255)` 的亮度是 158 > 阈值 140。）

### 修复
文档头由常量改成函数 `richDocHead()`，渲染时
`getComputedStyle(document.documentElement)` 读当前主题变量注入 srcdoc
（带浅色兜底值）。探针**单独把 iframe 拆开量**：
深色下实测 `iframe 底 = rgb(22, 28, 38)`、正文 `rgb(230, 234, 240)`；
采样脚本同时改成"透明的元素单独列出来报告"+"品牌色按颜色值放行"。

> ★ **但这一条当时被我错误地宣布成了"用户那块白的定案"** ——
> 用户复测后说"还是在"。真正那块白是第 42 条的 `.cc-greeting`。
> **我找到了一个真 bug ≠ 我找到了用户说的那个 bug。**

> 教训①：**iframe / 子文档 / canvas 里的颜色，主题变量化管不到**，
> 必须显式把值传进去。
> 教训②：**"没测到"会被报告成"通过"** —— 采样脚本必须把"跳过了哪些"
> 一起打印出来，否则漏检与通过长得一模一样。

---

## 42. 渐变躲过了"背景色亮度"检查：那块白其实在 `background-image` 里

### 现象
第 41 条修完（iframe 底色跟着主题走），用户刷新后回报：
"**深色主题下角色卡的白还是在**"。

### 根因
角色卡列表里每张卡的**开场白预览框**：

```css
.cc-greeting {
  background: linear-gradient(180deg, #f7f9fc, #f2f5fa);  /* 写死的浅色渐变 */
  color: #4a5568;                                          /* 写死的深灰字 */
}
```

深色主题下它就是每张卡上一整块浅色框。它躲过检查有**两个**原因，缺一不可：

1. **渐变画在 `background-image` 上**，`getComputedStyle(el).backgroundColor`
   读出来是 `rgba(0, 0, 0, 0)` —— "只比背景色亮度"的脚本把它当成透明块**跳过**；
2. 采样清单里**根本没有** `.cc-greeting` 这个选择器。

于是"没测到"被报告成了"通过"——**这是最危险的一类假绿**。

### 修复
```css
background: linear-gradient(180deg, var(--surface-3), var(--surface-2));
color: var(--text-dim);
```
（顺带把 `.cc-avatar` 的 `inset 0 0 0 3px #fff` 改成 `var(--surface)`，深色下不再是一圈白环。）

采样器升级为**渐变感知**：解析 `background-image` 里所有色标的 RGB，
取**最亮**的那个参与判断（报告里带 `grad` 前缀，一眼看得出是渐变）；
`.cc-greeting` 进**必采清单**（缺了直接判失败）。
另外加了通用静态守门人 `test_no_hardcoded_light_backgrounds_in_stylesheet`：
`:root` 之外的 `background*` 里，凡是亮于 200 的十六进制颜色或高不透明度的白，
一律判失败（`var(...)` 与低透明度提亮叠加放行）。

用桩在 Node 里验证过这条采样逻辑：喂进旧的写死渐变，输出
`.cc-greeting=grad249` → 亮度 249 > 阈值 → **判亮、断言失败**。

> 教训①：**"背景色"不等于"看起来的颜色"** —— 渐变、`background-image`、
> 伪元素、子文档都画在别的地方，检查必须覆盖它们（否则就是自欺）。
> 教训②：**先让用户确认位置，再宣布定案**。这次绕了一圈，代价是用户多跑了一轮。

### 后续：同一课又踩了两次（边框 与 "我找到的那几条"）

清完 `.cc-greeting` 之后顺手全量扫了一遍，又抓到两处：

1. **边框**：`.alert.warn/.danger/.info/.ok` 与 `.msg.failed .msg-body` 的 `border-color`
   写死成四个浅色（`#f0d9a0`/`#f3c2bb`/`#c8d3f7`/`#b7e3cd`）—— 背景早就是 `--*-soft`
   变量、能跟主题变，**只有边框留在浅色**，于是深色主题下每个提示框镶一圈**亮边**。
   收进 `--{warn,danger,info,ok}-border` 之后由引擎给老主题按
   "文字色 × 柔和底"的中间调补（`color-mix`）。
2. **漏报**：我第一次只报了 3 条，实际是 5 条 —— 因为我只 grep 了
   `warn` / `danger` / `failed` 三个词，`.alert.info` 与 `.alert.ok` 就这么漏掉了。
   **"我找到的那几条"不等于"全部"**：要么按属性全量扫（`border-color`），
   要么就别写"共 N 条"。
3. **"一半走变量"第三例**：目录里 ✦ 头像写成
   `style="background:var(--brand-soft);color:#6b46c1"` —— 底色跟着主题变、
   字色不变，深色下深紫压在深藏青上**看不清**（不是白块，是"读不出"）。
   收进 `--catalog-accent`。三次事故的共同点是：**同一处配色里有一半是变量**，
   而人工复查最容易放过这种地方（看着"已经变量化了"）。
   于是补了一条通用守门人：**底色是 `var(...)`、同一处的文字/边框色写死深色 → 判失败**。
   ★ 这条规则第一版写宽了（凡 `var()` + 深色就报），一跑就把代码块、请求日志抽屉这些
   "永远深色"的正当部件全误报了 —— **守门人必须收窄到真正的模式**，
   否则它会被当成噪音然后被关掉。

> ★ 最值得记的一条：**守门人是分画法的**。
> 扫 `background*` 的守门人**看不见边框**；量背景色的探针**看不见渐变**；
> 父页面的采样**看不见 iframe 子文档**。
> 每换一种"画法"就要补一条对应的断言，否则"没测到"永远会被报告成"通过"。

---

## 附：本项目用到的排查手法


| 手法 | 用途 |
|---|---|
| `httpx.MockTransport` | 不联网就能测协议解析、错误码、SSE 流 |
| 后台线程 + `join(timeout)` | 测试死锁而不让测试进程卡死 |
| 双轨对照实验 | 把变量逐个剥离，定位真正触发条件（本次死锁排查中起了决定性作用） |
| 真删一次验证外键语义 | ORM 的默认行为是否与外键动作一致，看代码看不出来，只能实测 |
| 正反两条回归测试 | 对外键这类「默认行为对错取决于配置」的地方，一正一反钉死 |
| 真实 API 实测 | 发现「文档里不会写」的行为：推理模型吃配额、参数被忽略 |
| 打印厂商原始响应 | 解析结果不对时，先看原始报文，别猜 |
| **本地假 OpenAI 服务**（`scripts/fake_openai_server.py`） | 不花钱、不联网地跑通「流式对话」全链路，也让毕设演示可复现 |
| **无头 Edge + DevTools 协议（CDP）** | `--virtual-time-budget --dump-dom` 会把虚拟时钟跑快、判断不了"逐字出现"，改用 CDP 在**真实时间**下每 120ms 采样 DOM，能得到文字增长曲线 |
| **常驻浏览器探针**（`scripts/ui_probe.py`） | 把上一轮的临时脚本变成资产：自己起假模型 + 造数据 + 点界面 + 清场。其中「**没有意外的失败请求**」这条断言抓出了 `…/sessions/null/regenerate` |
| **先打印真实请求地址** | 接不通第三方服务时，第一件事是把"我到底请求了哪个 URL"摆出来（第 16 条），比反复猜模型名快得多 |
