# zammad_translations.rb - custom Simplified Chinese overrides for strings
# the shipped Zammad 7.2 zh-cn language pack leaves in English (default
# overview names, dashboard widget titles, sidebar doc links).
# Executed INSIDE the railsserver container:
#   bundle exec rails runner /tmp/zammad_translations.rb
# Idempotent: re-running updates the same rows. After running, log out,
# log back in and hard-refresh (Ctrl+F5) to pick the new strings up.
# Console output is English-only on purpose; Chinese values are stored
# in the database from this file.
PAIRS = {
  'My Assigned Tickets' => '我受理的工单',
  'Unassigned & Open Tickets' => '未分配的待处理工单',
  'My Pending Reached Tickets' => '我的挂起提醒工单',
  'My Subscribed Tickets' => '我订阅的工单',
  'My Tickets in Process' => '我的处理中工单',
  'My handling time' => '我的处理用时',
  'minutes' => '分钟',
  'Average' => '平均',
  'Admin Documentation' => '管理员文档',
  'User Documentation' => '用户文档',
  # Shipped zh-cn pack mistranslates this one into Traditional characters
  # (輸入名字或電話號碼 快搜用戶.機構名稱); override with Simplified.
  'Enter Person or Organization/Company' => '输入名字或电话号码，快搜用户/机构名称',
  # Admin sidebar items the pack leaves Traditional (also covered by the
  # mechanical fix_traditional.rb sweep; these explicit rows pin them).
  'System' => '系统',
  'Voice' => '语音',
  # English leftovers found by the diag_translations.rb sweep (30 rows).
  # Format strings and placeholder patterns are deliberately left alone.
  '%s of %s' => '%s / %s',
  'API Key' => 'API 密钥',
  'API Token' => 'API 令牌',
  'API token' => 'API 令牌',
  'App ID' => '应用 ID',
  'App Secret' => '应用私钥',
  'App Tenant ID' => '应用租户 ID',
  'CTI (generic)' => 'CTI（通用）',
  'CTI Token' => 'CTI 令牌',
  'Company Inc.' => '示例公司',
  'Confirm new password' => '确认新密码',
  'Create organizations automatically if record has one.' => '如果记录有组织，自动创建组织。',
  'Data Privacy' => '数据隐私',
  'Exchange OAuth' => 'Exchange 授权',
  'Exchange config' => 'Exchange 配置',
  'GitHub config' => 'GitHub 配置',
  'John Smith' => '约翰·史密斯',
  'Placetel Token' => 'Placetel 令牌',
  'Powered by' => '技术支持',
  'Reset to default' => '恢复默认',
  'Set as default' => '设为默认',
  'Some Text' => '示例文本',
  'Twitter App Credentials' => 'Twitter 应用凭证',
  'Twitter Key' => 'Twitter 密钥',
  'Twitter Secret' => 'Twitter 私钥',
  'Zammad Helpdesk' => 'Zammad 客服系统',
  'i-doit config' => 'i-doit 配置',
  'sipgate.io config' => 'sipgate.io 配置',
  # AI Ticket Summary page (feature itself is off and unused in phase 1,
  # but its page strings were showing in English).
  'Ticket Summary' => '工单摘要',
  'Summary Services' => '摘要服务',
  'Customer Intent' => '客户意图',
  'Provide a summary of the problem the customer needs to get resolved.' => '总结客户需要解决的问题。',
  'Conversation Summary' => '对话摘要',
  'Provide a summary of the conversation between customer and support agent.' => '总结客户与客服坐席之间的对话。',
  'Open Questions' => '待解决问题',
  'Provide a summary of the questions raised in the conversation.' => '总结对话中提出的问题。',
  'Upcoming Events' => '后续事项',
  'Provide a summary of the upcoming events based on the conversation.' => '根据对话总结后续事项。',
  'Customer Sentiment' => '客户情绪',
  'Provide an assessment of the customer sentiment based on the conversation.' => '根据对话评估客户情绪。',
  'Summary Selector' => '摘要适用范围',
  'Defines which tickets can use the ticket summary sidebar.' => '定义哪些工单可以使用工单摘要侧栏。',
  # Data-privacy delete dialog: the shipped pack leaves these in English,
  # and its translation of the confirm sentence is actively misleading
  # (it says to type "删除", but Zammad's check only accepts DELETE).
  # This override keeps the instruction Chinese AND names the word the
  # check actually wants.
  'There is no rollback of this deletion possible. If you are sure that you wish to proceed, please type "DELETE" into the input.' => '删除操作无法撤销。如果确认要继续，请在输入框中输入 DELETE。',
  'matches found' => '条匹配',
  'invalid' => '无效',
}.freeze

# New Translation rows require created_by/updated_by (NOT NULL columns);
# existing rows already carry them, which is why only brand-new sources
# failed on the first run. Point them at the first admin user (id 1 is
# the internal system user and always exists).
ACTOR_ID = begin
  admin = User.all.reject { |u| u.id == 1 }.find do |u|
    u.roles.any? { |r| r.permissions.any? { |p| p.name == 'admin' } }
  end
  (admin && admin.id) || 1
rescue StandardError
  1
end

PAIRS.each do |source, target|
  begin
    rec = Translation.find_or_initialize_by(locale: 'zh-cn', source: source)
    rec.target = target
    rec.created_by_id ||= ACTOR_ID
    rec.updated_by_id = ACTOR_ID
    rec.save!
    puts "translation ok: #{source}"
  rescue StandardError => e
    puts "translation FAILED: #{source} (#{e.class}: #{e.message.to_s[0, 100]})"
  end
end
# New rows can sit behind the Rails cache (the dashboard kept showing the
# old English strings after the first run); flush it so the overrides
# take effect on next page load. The cache rebuilds on demand.
begin
  Rails.cache.clear
  puts 'cache cleared'
rescue StandardError => e
  puts "cache clear FAILED: #{e.class}"
end
puts 'TRANSLATIONS_DONE'
