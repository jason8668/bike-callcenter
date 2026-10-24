# zammad_cn_setup.rb - one-shot configuration for the bicycle
# customer-service trial instance. Executed INSIDE the Zammad railsserver
# container by deploy/apply-cn-setup.sh:
#   bundle exec rails runner /tmp/zammad_cn_setup.rb
#
# What it does:
#   1. System default language -> Simplified Chinese (zh-cn),
#      timezone -> Asia/Shanghai, API token access -> on.
#   2. Every existing user's UI language -> zh-cn.
#   3. Foreign integrations and third-party logins (Facebook, Twitter/X,
#      Telegram, WhatsApp, Google, Microsoft, LinkedIn, GitHub, GitLab,
#      SAML, OpenID Connect, Slack, ...) -> switched OFF; any configured
#      social/SMS channel accounts -> deactivated. Domestic channels
#      (phone/CTI, email, web) stay untouched.
#   4. Seeded English names -> mainland-Chinese display names:
#      ticket states, priorities, the default "Users" group.
#      (Only renames; nothing is deleted, all ids stay the same.)
#   5. Creates the bridge middleware API token + CTI token and prints
#      them as API_TOKEN=... / CTI_TOKEN=... lines for the caller script
#      (the caller writes them into middleware/.env, never displays them).
#
# Console output is English-only on purpose (the VNC console cannot show
# CJK glyphs); Chinese values are stored in the database from this file.
report = []

def apply_setting(report, name, value)
  if Setting.exists?(name: name)
    Setting.set(name, value)
    report << "set #{name} = #{value.inspect}"
  else
    report << "skip (setting not present): #{name}"
  end
rescue StandardError => e
  report << "FAILED setting #{name}: #{e.class}"
end

# --- 1. system language / timezone / API access -------------------------
apply_setting(report, 'locale_default', 'zh-cn')
apply_setting(report, 'timezone_default', 'Asia/Shanghai')
apply_setting(report, 'api_token_access', true)

# --- 2. every existing user -> zh-cn UI ---------------------------------
begin
  changed = 0
  User.find_each do |u|
    next if u.id == 1 # internal system user
    prefs = (u.preferences || {}).dup
    next if prefs['locale'] == 'zh-cn' || prefs[:locale] == 'zh-cn'
    prefs['locale'] = 'zh-cn'
    prefs.delete(:locale)
    u.preferences = prefs
    u.save!(validate: false)
    changed += 1
  end
  report << "user UI language -> zh-cn (#{changed} users updated)"
rescue StandardError => e
  report << "FAILED user locales: #{e.class}"
end

# --- 3. foreign integrations / logins / channels OFF -------------------
BRAND_WORDS = %w[facebook twitter telegram whatsapp google microsoft
                 linkedin github gitlab saml openid slack icinga nagios
                 monit sipgate placetel twilio clearbit].freeze
begin
  Setting.all.each do |s|
    lname = s.name.to_s.downcase
    next unless BRAND_WORDS.any? { |w| lname.include?(w) }
    value = begin
      s.state_current[:value]
    rescue StandardError
      nil
    end
    next unless value == true
    Setting.set(s.name, false)
    report << "disabled foreign setting: #{s.name}"
  end
rescue StandardError => e
  report << "FAILED disabling foreign settings: #{e.class}"
end

begin
  Channel.all.each do |c|
    area = c.area.to_s
    next unless area.match?(/facebook|twitter|telegram|whatsapp|sms|google|microsoft/i)
    next unless c.active
    c.active = false
    c.save!(validate: false)
    report << "deactivated channel: #{area}"
  end
rescue StandardError => e
  report << "FAILED channel cleanup: #{e.class}"
end

# --- 4. seeded names -> mainland Chinese --------------------------------
STATE_NAMES = {
  'new' => '新建', 'open' => '处理中', 'pending reminder' => '待回访',
  'pending close' => '待确认办结', 'closed' => '已办结',
  'merged' => '已合并', 'removed' => '已删除'
}.freeze
begin
  TicketState.all.each do |st|
    target = STATE_NAMES[st.name]
    next unless target
    st.name = target
    st.save!(validate: false)
    report << "ticket state renamed (id #{st.id})"
  end
rescue StandardError => e
  report << "FAILED state rename: #{e.class}"
end

PRIO_NAMES = { '1 low' => '低', '2 normal' => '普通', '3 high' => '高' }.freeze
begin
  TicketPriority.all.each do |p|
    target = PRIO_NAMES[p.name]
    next unless target
    p.name = target
    p.save!(validate: false)
    report << "ticket priority renamed (id #{p.id})"
  end
rescue StandardError => e
  report << "FAILED priority rename: #{e.class}"
end

begin
  g = Group.find_by(name: 'Users')
  if g && !Group.exists?(name: '默认组')
    g.name = '默认组'
    g.save!(validate: false)
    report << 'default group renamed (id ' + g.id.to_s + ')'
  end
rescue StandardError => e
  report << "FAILED group rename: #{e.class}"
end

# --- 5. bridge tokens -----------------------------------------------------
# NOTE: this block must never abort the script: if token setup fails, the
# apply wrapper needs the report lines below (not a bare traceback) to
# tell the operator what broke, and a re-run stays safe (idempotent).
begin
  admin = User.all.reject { |u| u.id == 1 }.find do |u|
    u.roles.any? { |r| r.permissions.any? { |p| p.name == 'admin' } }
  end
  admin ||= User.find_by(id: 2) || User.last
  begin
    report << "tokens table exists=#{ActiveRecord::Base.connection.table_exists?(:tokens)}"
  rescue StandardError => e
    report << "FAILED tokens table check: #{e.class}"
  end
  begin
    Token.where(label: 'bike-bridge').destroy_all
  rescue StandardError => e
    # Callbacks on destroy can fail on some versions; the rows are plain
    # auth tokens with no dependents, so a callback-free delete is safe.
    report << "token destroy_all failed (#{e.class}), trying delete_all"
    begin
      Token.where(label: 'bike-bridge').delete_all
    rescue StandardError => e2
      report << "FAILED token cleanup: #{e2.class}: #{e2.message.to_s[0, 120]}"
    end
  end
  token = Token.create!(user_id: admin.id, persistent: true, action: 'api',
                        label: 'bike-bridge',
                        preferences: { 'permission' => ['admin', 'ticket.agent'] })
  puts "API_TOKEN=#{token.name}"
rescue StandardError => e
  report << "FAILED token setup: #{e.class}: #{e.message.to_s[0, 120]}"
end

begin
  cti = Setting.get('cti_token')
rescue StandardError
  cti = nil
end
if cti.nil? || cti.to_s.empty?
  begin
    cti = SecureRandom.urlsafe_base64(24)
    Setting.set('cti_token', cti)
  rescue StandardError => e
    report << "FAILED cti_token setup: #{e.class}"
    cti = nil
  end
end
puts "CTI_TOKEN=#{cti}" if cti

puts '--- cn setup report ---'
report.each { |line| puts line }
puts 'CN_SETUP_DONE'
