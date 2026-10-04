-- 本文件由 scripts/init_db.py --dump-sql 自动生成，请勿手工修改。
-- 表结构的唯一事实来源是 app/db/models/ 下的 ORM 模型。

CREATE TABLE users (
	id BIGINT NOT NULL COMMENT '用户ID' AUTO_INCREMENT, 
	username VARCHAR(50) NOT NULL COMMENT '登录用户名', 
	email VARCHAR(255) NOT NULL COMMENT '邮箱', 
	password_hash VARCHAR(255) NOT NULL COMMENT 'bcrypt 密码哈希（不可逆，禁止存明文）', 
	nickname VARCHAR(50) COMMENT '昵称（可空，未填时展示用户名）', 
	is_active BOOL NOT NULL COMMENT '是否启用（禁用后无法登录）' DEFAULT 1, 
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
	context_window INTEGER NOT NULL COMMENT '模型上下文窗口总容量（输入 + 输出）' DEFAULT 65536, 
	temperature FLOAT NOT NULL COMMENT '采样温度 0~2，叙事场景建议 0.7~1.0' DEFAULT 0.8, 
	top_p FLOAT COMMENT '核采样 top_p，留空表示不发送该参数', 
	max_tokens INTEGER NOT NULL COMMENT '最大输出 token —— 包含思考过程 token，不是正文字数上限' DEFAULT 2048, 
	reasoning_effort VARCHAR(16) NOT NULL COMMENT '思考强度：auto / off / low / medium / high' DEFAULT 'auto', 
	reasoning_effort_supported BOOL COMMENT '实测该模型是否支持思考强度。NULL = 尚未探测', 
	reasoning_effort_probed_model VARCHAR(128) COMMENT '探测时使用的模型名 —— 换模型后旧结论即失效，用于判断过期', 
	reasoning_effort_probed_at DATETIME COMMENT '思考强度支持情况的探测时间', 
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
	title VARCHAR(200) NOT NULL COMMENT '会话标题（默认取角色卡名+时间，可重命名）', 
	status VARCHAR(20) NOT NULL COMMENT '会话状态：active / archived' DEFAULT 'active', 
	rolling_summary MEDIUMTEXT COMMENT '剧情滚动摘要（压缩旧对话，控制上下文长度）', 
	summarized_until_message_id BIGINT COMMENT '摘要已覆盖到的最后一条消息ID', 
	message_count INTEGER NOT NULL COMMENT '消息总数' DEFAULT 0, 
	total_tokens INTEGER NOT NULL COMMENT '累计消耗 token 数（用于展示用量）' DEFAULT 0, 
	last_active_at DATETIME COMMENT '最后活跃时间（用于会话列表排序）', 
	created_at DATETIME NOT NULL COMMENT '创建时间' DEFAULT now(), 
	updated_at DATETIME NOT NULL COMMENT '更新时间' DEFAULT now(), 
	PRIMARY KEY (id), 
	FOREIGN KEY(user_id) REFERENCES users (id) ON DELETE CASCADE, 
	FOREIGN KEY(character_card_id) REFERENCES character_cards (id) ON DELETE SET NULL, 
	FOREIGN KEY(llm_provider_id) REFERENCES llm_providers (id) ON DELETE SET NULL
)COMMENT='叙事会话表';

CREATE TABLE messages (
	id BIGINT NOT NULL COMMENT '消息ID' AUTO_INCREMENT, 
	session_id BIGINT NOT NULL COMMENT '所属会话ID', 
	`role` VARCHAR(16) NOT NULL COMMENT '角色：system / user / assistant', 
	content MEDIUMTEXT NOT NULL COMMENT '消息正文', 
	token_count INTEGER NOT NULL COMMENT '本条消息的 token 数', 
	model_name VARCHAR(128) COMMENT '生成该消息所用模型名（用户消息为 NULL）', 
	latency_ms INTEGER COMMENT '模型响应耗时（毫秒），用于性能分析', 
	created_at DATETIME NOT NULL COMMENT '创建时间' DEFAULT now(), 
	PRIMARY KEY (id), 
	FOREIGN KEY(session_id) REFERENCES narrative_sessions (id) ON DELETE CASCADE
)COMMENT='消息表';
