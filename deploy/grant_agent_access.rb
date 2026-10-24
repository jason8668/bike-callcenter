# grant_agent_access.rb - give the bridge/API user ticket-agent powers.
# Why: Zammad's TicketPolicy requires ticket.agent permission AND group
# access to create a ticket. The first-run admin user has neither by
# default (the Admin role alone is not enough), so ticket creation via
# the bridge API fails with 403 while admin calls (users, groups, fields)
# succeed with the same token.
# v2 fix (2026-10-24): the v1 script wrote group rows with raw SQL that
# selected the groups_users.id column - but that join table HAS NO id
# column (group_id + user_id + access only), so every group grant failed
# with PG::UndefinedColumn and the rows were never written (diag kept
# showing NO ROW). This version uses Zammad's own model API instead:
# User#group_ids_access_map= writes the join rows correctly, no SQL.
# Targets the bike-bridge token owner first (the account that actually
# creates tickets), falling back to the first non-system admin.
# Idempotent. Run inside the railsserver container:
#   bundle exec rails runner /tmp/grant_agent_access.rb
# Console output is English-only on purpose.
token = Token.find_by(name: 'bike-bridge')
user = token&.user
if user.nil?
  user = User.all.reject { |u| u.id == 1 }.find do |u|
    u.roles.any? { |r| r.permissions.any? { |p| p.name == 'admin' } }
  end
end
if user.nil?
  puts 'ERROR: no API user found'
  exit 1
end
puts "user: #{user.login} (id=#{user.id})"
puts "roles before: #{user.roles.map(&:name).join(',')}"
agent_role = Role.find_by(name: 'Agent')
if agent_role.nil?
  puts 'ERROR: Agent role not found'
  exit 1
end
if user.roles.map(&:id).include?(agent_role.id)
  puts 'Agent role already present'
else
  user.roles << agent_role
  puts 'Agent role added'
end
groups = Group.where(active: true)
if groups.empty?
  puts 'WARNING: no active groups found, nothing to grant'
else
  access_map = {}
  groups.each { |g| access_map[g.id] = 'full' }
  user.group_ids_access_map = access_map
  user.save!
  groups.each do |g|
    puts "group access granted full: #{g.name} (id=#{g.id})"
  end
  puts "group_ids_access_map now: #{user.group_ids_access_map.inspect}"
end
puts 'GRANT_AGENT_DONE'
