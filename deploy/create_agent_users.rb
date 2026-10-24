# create_agent_users.rb - create agent accounts from a simple list, no
# first/last-name split: each account is just a username + password.
#
# Input file /tmp/agents.txt inside the container, one per line:
#   username password [email]
# (username and password must not contain spaces; email is optional.)
# Each account gets login=username, firstname=username, the Agent role
# and membership in 客服组, so signing in is simply username + password.
# Existing logins are skipped. Delete the list file afterwards: it holds
# passwords in plain text.
#
#   docker compose exec -T zammad-railsserver bundle exec rails runner /tmp/create_agent_users.rb
# Console output is English-only on purpose.
agent_role = Role.find_by(name: 'Agent')
service_group = Group.find_by(name: '客服组')
if agent_role.nil? || service_group.nil?
  puts 'ERROR: Agent role or 客服组 missing, aborting'
  exit 1
end
unless File.exist?('/tmp/agents.txt')
  puts 'ERROR: /tmp/agents.txt not found (copy it into the container first)'
  exit 1
end
conn = ActiveRecord::Base.connection
created = 0
File.foreach('/tmp/agents.txt') do |line|
  parts = line.strip.split(/\s+/)
  next if parts.empty? || parts[0].start_with?('#')
  username, password, email = parts
  if username.nil? || password.nil?
    puts "skip (need username and password): #{line.strip}"
    next
  end
  if User.find_by(login: username)
    puts "skip (exists): #{username}"
    next
  end
  u = User.new(login: username, firstname: username, lastname: '',
               roles: [agent_role])
  u.email = email if email
  u.password = password
  unless u.save
    puts "FAILED #{username}: #{u.errors.full_messages.join('; ').to_s[0, 200]}"
    next
  end
  row = conn.select_one(
    "SELECT id FROM groups_users WHERE user_id = #{u.id} AND group_id = #{service_group.id}"
  )
  unless row
    conn.execute(
      "INSERT INTO groups_users (user_id, group_id, access, created_at, updated_at) " \
      "VALUES (#{u.id}, #{service_group.id}, 'full', NOW(), NOW())"
    )
  end
  created += 1
  puts "created agent: #{username}"
end
puts "CREATE_AGENTS_DONE (#{created} new)"
