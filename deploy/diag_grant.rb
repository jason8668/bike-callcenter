# diag_grant.rb - value-free check: does the bridge/API user actually have
# ticket-agent powers? Prints roles, the ticket.agent permission, and per-group
# access for the user who owns the 'bike-bridge' token. Never prints tokens
# or secrets. Run inside the railsserver container:
#   bundle exec rails runner /tmp/diag_grant.rb
token = Token.find_by(name: 'bike-bridge')
if token.nil?
  puts 'ERROR: bike-bridge token not found'
  exit 1
end
user = token.user
if user.nil?
  puts 'ERROR: bike-bridge token has no user'
  exit 1
end
puts "token user: #{user.login} (id=#{user.id})"
puts "roles: #{user.roles.map(&:name).join(',')}"
begin
  puts "ticket.agent permission: #{user.permissions?('ticket.agent')}"
rescue StandardError => e
  puts "ticket.agent permission: unknown (#{e.class})"
end
conn = ActiveRecord::Base.connection
Group.where(active: true).each do |g|
  row = conn.select_one(
    "SELECT access FROM groups_users WHERE user_id = #{user.id} AND group_id = #{g.id}"
  )
  puts "group #{g.name}: #{row ? row['access'] : 'NO ROW'}"
end
puts 'DIAG_GRANT_DONE'
