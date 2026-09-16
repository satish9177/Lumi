-- Runs once, when the data volume is first initialized. The agent test suite
-- truncates its database, so it gets its own and never touches lumi_agent.
CREATE DATABASE lumi_agent_test;
