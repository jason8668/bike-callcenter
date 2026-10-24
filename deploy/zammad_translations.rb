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
