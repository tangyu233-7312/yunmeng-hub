-- 本文件由 scripts/init_db.py --dump-sql 自动生成，请勿手工修改。
-- 表结构的唯一事实来源是 app/db/models/ 下的 ORM 模型。

CREATE TABLE users (
	id BIGINT NOT NULL COMMENT '用户ID' AUTO_INCREMENT, 
	username VARCHAR(50) NOT NULL COMMENT '登录用户名', 
	email VARCHAR(255) NOT NULL COMMENT '邮箱', 
	password_hash VARCHAR(255) NOT NULL COMMENT 'bcrypt 密码哈希（不可逆，禁止存明文）', 
	nickname VARCHAR(50) COMMENT '昵称（可空，未填时展示用户名）', 
	is_active BOOL NOT NULL COMMENT '是否启用（禁用后无法登录）' DEFAULT 1, 
	builtin_preset_dismissed BOOL NOT NULL COMMENT '是否已删除内置守卫预设（删除后不再自动重建，直到点「还原」）' DEFAULT 0, 
	plugin_defaults_seeded BOOL NOT NULL COMMENT '是否已初始化默认插件（删掉后不再自动重建）' DEFAULT 0, 
	created_at DATETIME NOT NULL COMMENT '创建时间' DEFAULT now(), 
	updated_at DATETIME NOT NULL COMMENT '更新时间' DEFAULT now(), 
	PRIMARY KEY (id)
)COMMENT='用户表';

CREATE TABLE llm_providers (
	id BIGINT NOT NULL COMMENT '配置ID' AUTO_INCREMENT, 
	user_id BIGINT NOT NULL COMMENT '所属用户ID', 
	name VARCHAR(64) NOT NULL COMMENT '用户起的别名，如「我的DeepSeek」', 
	provider_type VARCHAR(32) NOT NULL COMMENT '协议类型，决定使用哪个适配器' DEFAULT 'openai_compatible', 
	base_url VARCHAR(512) NOT NULL COMMENT 'API 基础地址，如 https://api.deepseek.com/v1', 
	api_key_encrypted VARCHAR(1024) NOT NULL COMMENT 'API Key 密文（Fernet 加密，禁止存明文）', 
	model_name VARCHAR(128) NOT NULL COMMENT '模型名，如 deepseek-flash', 
	fallback_provider_id BIGINT COMMENT '备用模型配置ID（主模型失败且尚未输出内容时自动切换）', 
	stream_enabled BOOL NOT NULL COMMENT '是否使用流式传输（默认开；关掉后等整段返回）' DEFAULT 1, 
	store_reasoning BOOL NOT NULL COMMENT '是否把推理模型的思考过程存进库（默认开；关掉则只在流式时显示一眼）' DEFAULT 1, 
	context_window INTEGER NOT NULL COMMENT '模型上下文窗口总容量（输入 + 输出）' DEFAULT 65536, 
	temperature FLOAT NOT NULL COMMENT '采样温度 0~2，叙事场景建议 0.7~1.0' DEFAULT 0.8, 
	top_p FLOAT COMMENT '核采样 top_p，留空表示不发送该参数', 
	max_tokens INTEGER NOT NULL COMMENT '最大输出 token —— 包含思考过程 token，不是正文字数上限' DEFAULT 2048, 
	reasoning_effort VARCHAR(16) NOT NULL COMMENT '思考强度：auto / off / low / medium / high' DEFAULT 'auto', 
	extra_params JSON NOT NULL COMMENT '长尾参数：厂商特有字段（如 response_format、presence_penalty）透传；以 _ 开头的键为本项目内部元数据，不会发送到上游', 
	is_default BOOL NOT NULL COMMENT '是否为该用户的默认模型（每个用户最多一个，由业务逻辑保证）' DEFAULT 0, 
	is_active BOOL NOT NULL COMMENT '是否启用' DEFAULT 1, 
	last_tested_at DATETIME COMMENT '最后一次连通性测试时间', 
	last_test_ok BOOL COMMENT '最后一次测试是否成功（NULL 表示从未测试）', 
	last_test_message VARCHAR(512) COMMENT '最后一次测试的失败原因摘要', 
	created_at DATETIME NOT NULL COMMENT '创建时间' DEFAULT now(), 
	updated_at DATETIME NOT NULL COMMENT '更新时间' DEFAULT now(), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_llm_provider_user_name UNIQUE (user_id, name), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)COMMENT='用户自配的大模型 API 配置表';

CREATE TABLE plugins (
	id BIGINT NOT NULL COMMENT '插件ID' AUTO_INCREMENT, 
	user_id BIGINT NOT NULL COMMENT '所属用户ID', 
	name VARCHAR(120) NOT NULL COMMENT '插件名（允许重名，理由同角色卡）', 
	kind VARCHAR(16) NOT NULL COMMENT '插件类型：regex / prompt / css / dice', 
	description VARCHAR(500) COMMENT '一句话说明这个插件做什么', 
	version VARCHAR(32) COMMENT '插件自报的版本号（纯展示）', 
	author VARCHAR(120) COMMENT '作者（纯展示，来源 URL 里的账号不一定是作者）', 
	source_url VARCHAR(500) COMMENT '安装来源 URL（GitHub 页面或 raw 地址）', 
	is_builtin BOOL NOT NULL COMMENT '是否为随账号初始化的默认插件' DEFAULT 0, 
	enabled BOOL NOT NULL COMMENT '是否启用' DEFAULT 1, 
	priority INTEGER NOT NULL COMMENT '应用顺序（小的先）' DEFAULT 100, 
	config JSON NOT NULL COMMENT '插件配置（regex.rules / prompt.{position,content} / css.css / dice.{triggers,…}）', 
	raw_manifest MEDIUMTEXT COMMENT '安装时的原始 JSON 清单（原样保存，便于审计）', 
	created_at DATETIME NOT NULL COMMENT '创建时间' DEFAULT now(), 
	updated_at DATETIME NOT NULL COMMENT '更新时间' DEFAULT now(), 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)COMMENT='插件表（声明式：正则替换 / 提示词注入 / CSS 主题）';

CREATE TABLE prompt_presets (
	id BIGINT NOT NULL COMMENT '预设ID' AUTO_INCREMENT, 
	user_id BIGINT NOT NULL COMMENT '所属用户ID', 
	name VARCHAR(120) NOT NULL COMMENT '预设名称（允许重名，理由同角色卡）', 
	description VARCHAR(500) COMMENT '一句话说明这套预设是干什么的', 
	config JSON NOT NULL COMMENT '块清单 + 顺序 + 采样参数（JSON）', 
	source_filename VARCHAR(255) COMMENT '导入来源文件名（便于追查这份预设从哪来）', 
	source_format VARCHAR(32) NOT NULL COMMENT '来源格式：hne / sillytavern' DEFAULT 'hne', 
	import_notes MEDIUMTEXT COMMENT '导入时的提醒与降级说明（换行分隔）', 
	is_active BOOL NOT NULL COMMENT '是否为该用户的全局默认预设' DEFAULT 0, 
	is_builtin BOOL NOT NULL COMMENT '是否为内置守卫预设（可编辑、可删除、可还原）' DEFAULT 0, 
	created_at DATETIME NOT NULL COMMENT '创建时间' DEFAULT now(), 
	updated_at DATETIME NOT NULL COMMENT '更新时间' DEFAULT now(), 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)COMMENT='提示词预设表（规范模型行为的规则/破甲/采样参数）';

CREATE TABLE world_books (
	id BIGINT NOT NULL COMMENT '世界书ID' AUTO_INCREMENT, 
	user_id BIGINT NOT NULL COMMENT '创建者用户ID', 
	name VARCHAR(200) COMMENT '世界书名称（可为空）', 
	description TEXT COMMENT '简介', 
	entries JSON NOT NULL COMMENT '条目数组（关键词触发的设定文本）', 
	extra_data JSON NOT NULL COMMENT '导入时未能映射到本表字段的原始数据（保证导出时不丢字段）', 
	created_at DATETIME NOT NULL COMMENT '创建时间' DEFAULT now(), 
	updated_at DATETIME NOT NULL COMMENT '更新时间' DEFAULT now(), 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE
)COMMENT='世界书表';

CREATE TABLE character_cards (
	id BIGINT NOT NULL COMMENT '角色卡ID' AUTO_INCREMENT, 
	user_id BIGINT NOT NULL COMMENT '创建者用户ID', 
	name VARCHAR(100) NOT NULL COMMENT '角色名', 
	avatar_url VARCHAR(512) COMMENT '头像地址', 
	description TEXT COMMENT '一句话简介', 
	personality TEXT COMMENT '性格特征', 
	background TEXT COMMENT '背景故事 / 身世设定', 
	speaking_style TEXT COMMENT '说话风格与语气', 
	scenario TEXT COMMENT '初始场景，即故事从哪里开始', 
	example_dialogue MEDIUMTEXT COMMENT '对话示例（few-shot 范例，用于稳定输出风格）', 
	greeting MEDIUMTEXT COMMENT '开场白（角色说的第一句话，故事从这里开始）', 
	alternate_greetings JSON NOT NULL COMMENT '备选开场白数组，用户可从中挑选一个开局', 
	system_prompt MEDIUMTEXT COMMENT '自定义系统提示词（留空则用引擎按人设字段自动拼装的那份）', 
	post_history_instructions MEDIUMTEXT COMMENT '尾注指令，追加在对话历史之后（用于强化文风或输出格式要求）', 
	tags JSON NOT NULL COMMENT '标签数组，如 [''奇幻'',''侦探'']', 
	extra_data JSON NOT NULL COMMENT '导入时未能映射到本表字段的原始数据（保证导出时不丢字段）', 
	is_public BOOL NOT NULL COMMENT '是否公开（公开后其他用户可选用该角色卡）' DEFAULT 0, 
	world_book_id BIGINT COMMENT '关联的世界书ID（可为空）', 
	created_at DATETIME NOT NULL COMMENT '创建时间' DEFAULT now(), 
	updated_at DATETIME NOT NULL COMMENT '更新时间' DEFAULT now(), 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(world_book_id) REFERENCES world_books (id) ON DELETE SET NULL
)COMMENT='角色卡表';

CREATE TABLE narrative_sessions (
	id BIGINT NOT NULL COMMENT '会话ID' AUTO_INCREMENT, 
	user_id BIGINT NOT NULL COMMENT '所属用户ID', 
	character_card_id BIGINT COMMENT '使用的角色卡ID（角色卡被删除后置空，会话本身保留）', 
	llm_provider_id BIGINT COMMENT '使用的模型配置ID（配置被删除后置空，会话本身保留）', 
	prompt_preset_id BIGINT COMMENT '本会话使用的提示词预设ID（空 = 用全局默认预设 / 内置装配）', 
	kind VARCHAR(16) NOT NULL COMMENT '会话类型：story（叙事）/ chat（纯聊天，无角色）' DEFAULT 'story', 
	branch_group_id BIGINT COMMENT '世界线组 ID（= 组内第一条线的会话 ID；NULL = 未参与分支）', 
	branch_index INTEGER NOT NULL COMMENT '世界线序号：0 主线 / 1 平行线' DEFAULT 0, 
	branch_label VARCHAR(64) COMMENT '世界线显示名（默认「主线」/「平行线」）', 
	branch_enabled BOOL NOT NULL COMMENT '是否开启对话分支模式（开启后才有生成另一条走向的入口）' DEFAULT 0, 
	branch_from_message_id BIGINT COMMENT '分叉点消息 ID（平行线从这条消息之后与主线分开）', 
	title VARCHAR(200) NOT NULL COMMENT '会话标题（默认取角色卡名+时间，可重命名）', 
	status VARCHAR(20) NOT NULL COMMENT '会话状态：active / archived' DEFAULT 'active', 
	rolling_summary MEDIUMTEXT COMMENT '剧情滚动总结（前情提要；覆盖区间见 summary_*_round）', 
	summarized_until_message_id BIGINT COMMENT '总结已覆盖到的最后一条消息ID', 
	summary_from_round INTEGER COMMENT '这份总结从第几轮开始覆盖（轮 = 一问一答）', 
	summary_to_round INTEGER COMMENT '这份总结覆盖到第几轮（也是下次从哪继续的依据）', 
	summary_settings_json MEDIUMTEXT COMMENT '记忆总结设置 JSON（见 app/narrative/summary.py 的 default_settings）', 
	summary_history_json MEDIUMTEXT COMMENT '总结的历史版本 JSON 数组（恢复上一次用）', 
	memory_anchors_json MEDIUMTEXT COMMENT '记忆锚点 JSON 数组（用户手写，固定注入、永不折叠）', 
	translate_settings_json MEDIUMTEXT COMMENT '翻译中间件设置 JSON（见 app/narrative/translate.py 的 DEFAULT_SETTINGS）', 
	state_json MEDIUMTEXT COMMENT '结构化状态 JSON（字段由 state_schema_json 定义，已校验）', 
	state_schema_json MEDIUMTEXT COMMENT '状态栏字段定义 JSON（含空 schema = 该卡未定义状态栏）', 
	message_count INTEGER NOT NULL COMMENT '消息总数' DEFAULT 0, 
	total_tokens INTEGER NOT NULL COMMENT '累计消耗 token 数（用于展示用量）' DEFAULT 0, 
	last_active_at DATETIME COMMENT '最后活跃时间（用于会话列表排序）', 
	created_at DATETIME NOT NULL COMMENT '创建时间' DEFAULT now(), 
	updated_at DATETIME NOT NULL COMMENT '更新时间' DEFAULT now(), 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(character_card_id) REFERENCES character_cards (id) ON DELETE SET NULL, 
	FOREIGN KEY(llm_provider_id) REFERENCES llm_providers (id) ON DELETE SET NULL, 
	FOREIGN KEY(prompt_preset_id) REFERENCES prompt_presets (id) ON DELETE SET NULL
)COMMENT='叙事会话表';

CREATE TABLE card_states (
	id BIGINT NOT NULL COMMENT '主键' AUTO_INCREMENT, 
	session_id BIGINT NOT NULL COMMENT '所属会话ID', 
	schema_hash VARCHAR(64) NOT NULL COMMENT '状态形状指纹（顶层键集合的哈希）', 
	data_json MEDIUMTEXT NOT NULL COMMENT '卡状态 JSON（原样，未校验）', 
	source VARCHAR(32) NOT NULL COMMENT '识别来源（见 app/narrative/card_state.py）', 
	raw_json MEDIUMTEXT COMMENT '最近一次的卡状态块原文', 
	message_id BIGINT COMMENT '最近一次写入来自哪条消息（排错用，不是外键）', 
	updates INTEGER NOT NULL COMMENT '累计写入次数', 
	prev_hash VARCHAR(64) COMMENT '上一轮的形状指纹（NULL = 首轮）', 
	drift_json MEDIUMTEXT COMMENT '最近一次字段消失/类型变化检查结果 JSON', 
	created_at DATETIME NOT NULL COMMENT '首次出现时间' DEFAULT now(), 
	updated_at DATETIME NOT NULL COMMENT '最近更新时间' DEFAULT now(), 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES narrative_sessions (id) ON DELETE CASCADE
)COMMENT='卡自带状态表（第二状态存储，不注入提示词）';

CREATE TABLE messages (
	id BIGINT NOT NULL COMMENT '消息ID' AUTO_INCREMENT, 
	session_id BIGINT NOT NULL COMMENT '所属会话ID', 
	`role` VARCHAR(16) NOT NULL COMMENT '角色：system / user / assistant', 
	content MEDIUMTEXT NOT NULL COMMENT '消息正文', 
	token_count INTEGER NOT NULL COMMENT '本条消息的 token 数', 
	model_name VARCHAR(128) COMMENT '生成该消息所用模型名（用户消息为 NULL）', 
	latency_ms INTEGER COMMENT '模型响应耗时（毫秒），用于性能分析', 
	state_meta_json TEXT COMMENT '状态遥测 JSON（漂移曲线的原始数据）', 
	rolls_json TEXT COMMENT '骰点 JSON 数组（骰子插件：点数与骰面）', 
	translation_json TEXT COMMENT '翻译中间件结果 JSON（原文/译文可切换）', 
	state_raw_json TEXT COMMENT '模型输出的 <state> 原始文本（界面「看作者原格式」用）', 
	usage_json TEXT COMMENT '本轮真实用量 JSON（厂商分项 + 我们的输入估算）', 
	reasoning TEXT COMMENT '推理模型的思考过程（reasoning_content）；普通模型为 NULL', 
	created_at DATETIME NOT NULL COMMENT '创建时间' DEFAULT now(), 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES narrative_sessions (id) ON DELETE CASCADE
)COMMENT='消息表';
