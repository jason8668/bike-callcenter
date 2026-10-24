# grant_agent_access.rb - give the bridge/API user ticket-agent powers.
# Why: Zammad's TicketPolicy requires ticket.agent permission AND group
# access to create a ticket. The first-run admin user has neither by
# default (the Admin role alone is not enough), so ticket creation via
# the bridge API fails with 403 while admin calls (users, groups, fields)
# succeed with the same token. This script adds the Agent role and full
# access to every active group to that user. Idempotent. Run inside the
# railsserver container:
#   bundle exec rails runner /tmp/grant_agent_access.rb
# Console output is English-only on purpose.
admin = User.all.reject { |u| u.id == 1 }.find do |u|
  u.roles.any? { |r| r.permissions.any? { |p| p.name == 'admin' } }
end
if admin.nil?
  puts 'ERROR: no admin user found'
  exit 1
end
puts "user: #{admin.login} (id=#{admin.id})"
puts "roles before: #{admin.roles.map(&:name).join(',')}"
agent_role = Role.find_by(name: 'Agent')
if agent_role.nil?
  puts 'ERROR: Agent role not found'
  exit 1
end
if admin.roles.map(&:id).include?(agent_role.id)
  puts 'Agent role already present'
else
  admin.roles << agent_role
  puts 'Agent role added'
end
conn = ActiveRecord::Base.connection
Group.where(active: true).each do |g|
  begin
    row = conn.select_one(
      "SELECT id FROM groups_users WHERE user_id = #{admin.id} AND group_id = #{g.id}"
    )
    if row
      conn.execute(
        "UPDATE groups_users SET access = 'full', updated_at = NOW() WHERE id = #{row['id']}"
      )
      puts "group access updated to full: #{g.name} (id=#{g.id})"
    else
      conn.execute(
        "INSERT INTO groups_users (user_id, group_id, access, created_at, updated_at) " \
        "VALUES (#{admin.id}, #{g.id}, 'full', NOW(), NOW())"
      )
      puts "group access granted full: #{g.name} (id=#{g.id})"
    end
  rescue StandardError => e
    puts "FAILED group #{g.name} (id=#{g.id}): #{e.class}: #{e.message.to_s[0, 100]}"
  end
end
puts 'GRANT_AGENT_DONE'
