extension radius
extension postgresql

param application string
param environment string
param databaseName string

resource database 'Demo.Platform/postgreSqlDatabases@2025-08-01-preview' = {
  name: 'postgres'
  properties: {
    application: application
    environment: environment
    databaseName: databaseName
  }
}

output databaseId string = database.id
