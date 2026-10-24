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
}.freeze

PAIRS.each do |source, target|
  begin
    rec = Translation.find_or_initialize_by(locale: 'zh-cn', source: source)
    rec.target = target
    rec.save!
    puts "translation ok: #{source}"
  rescue StandardError => e
    puts "translation FAILED: #{source} (#{e.class}: #{e.message.to_s[0, 100]})"
  end
end
puts 'TRANSLATIONS_DONE'
